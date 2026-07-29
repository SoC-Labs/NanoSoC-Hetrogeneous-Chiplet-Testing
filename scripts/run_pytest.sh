#!/usr/bin/env bash
#-----------------------------------------------------------------------------
# scripts/run_pytest.sh — the single place that turns a TEST LEVEL into a
#                         pytest marker expression.
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
#   ./scripts/run_pytest.sh --level l0
#   ./scripts/run_pytest.sh --level l3 -- -x --tb=short
#
# LEVEL → MARKER, from docs/REPO_LAYOUT.md. Markers are the contract with
# tests/ (which another area owns), so they are written down once, here:
#
#   l0     not hardware                                     no boards
#   l1l2   hardware and single_board and not pair and not data_plane and not soak
#   l3     hardware and pair and not data_plane and not soak
#   l4     data_plane                                       ATTENDED ONLY
#   l5     soak                                             ATTENDED ONLY
#
# Two properties this file exists to guarantee:
#
#  1. A MISSING FRAMEWORK IS A MESSAGE, NOT A TRACEBACK. Until host/hetsoc/ and
#     tests/ land, `make test-offline` must say what is missing in one line.
#     A 40-line ImportError chain from pytest's collector teaches nobody
#     anything, and in CI it looks like a real regression.
#
#  2. THE L4/L5 GATE CANNOT BE ROUTED AROUND. The opt-in is checked here, in
#     the runner, not in the Makefile — so calling the script directly is
#     exactly as guarded as `make test-dataplane`.
#-----------------------------------------------------------------------------
set -euo pipefail
# shellcheck source=_common.sh
. "$(dirname "$(readlink -f "$0")")/_common.sh"

LEVEL=""
declare -a EXTRA=()

while [ "$#" -gt 0 ]; do
    case "$1" in
        --level) LEVEL="${2:-}"; shift 2 ;;
        --)      shift; EXTRA=("$@"); break ;;
        -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
        *)       err "unknown option '$1'"; exit 2 ;;
    esac
done
[ -n "${LEVEL}" ] || { err "usage: run_pytest.sh --level l0|l1l2|l3|l4|l5 [-- pytest args]"; exit 2; }

# LEVEL_OPTS: options the SUITE itself defines (tests/conftest.py), as opposed
# to the marker expression. The suite deselects every `data_plane`/`soak` test
# unless `--data-plane` is passed — a second, independent safety interlock on
# top of I_ACCEPT_WEDGE_RISK. Selecting the marker alone collects nothing, so
# both have to be set, and they are set in different places on purpose: one
# guards the operator, one guards the suite.
declare -a LEVEL_OPTS=()

case "${LEVEL}" in
    l0)   MARKER='not hardware'
          LABEL='L0 offline — host logic only, no boards'
          ATTENDED=0 ;;
    l1l2) MARKER='hardware and single_board and not pair and not data_plane and not soak'
          LABEL='L1+L2 single board — read-only probes, then config-plane writes'
          ATTENDED=0 ;;
    l3)   MARKER='hardware and pair and not data_plane and not soak'
          LABEL='L3 pair — link bring-up + cross-die control plane'
          ATTENDED=0 ;;
    l4)   MARKER='data_plane'
          LABEL='L4 cross-die DATA PLANE'
          LEVEL_OPTS=(--data-plane)
          ATTENDED=1 ;;
    l5)   MARKER='soak'
          LABEL='L5 soak / stress / characterisation'
          LEVEL_OPTS=(--data-plane "--soak-iters=${HETSOC_SOAK_ITERS:-1000}")
          ATTENDED=1 ;;
    *)    err "unknown level '${LEVEL}' (l0|l1l2|l3|l4|l5)"; exit 2 ;;
esac

# The peer read-back round-trip is the single most wedge-prone thing the suite
# can do, so the suite hides it behind its own third flag. Never defaulted on.
if [ "${HETSOC_ALLOW_PEER_READ:-0}" = "1" ] && [ "${ATTENDED}" -eq 1 ]; then
    LEVEL_OPTS+=(--allow-peer-read)
fi

# --- the attended gate -------------------------------------------------------
if [ "${ATTENDED}" -eq 1 ]; then
    require_wedge_optin "${LABEL}"
fi

# --- environment sanity, with human-readable failures ------------------------
PY="$(hetsoc_python)"

if [ ! -x "${PY}" ] && ! have "${PY}"; then
    err "no usable python interpreter."
    err "  fix: source set_env.sh && make deps"
    exit 1
fi

if [ ! -x "${HETSOC_VENV}/bin/python" ]; then
    err "no virtualenv at ${HETSOC_VENV}."
    err "  fix: make deps        (creates .venv and pip installs host/)"
    exit 1
fi

if ! "${PY}" -c 'import pytest' 2>/dev/null; then
    err "pytest is not installed in ${HETSOC_VENV}."
    err "  fix: make deps        (or: ${HETSOC_VENV}/bin/pip install pytest)"
    exit 1
fi

if ! "${PY}" -c 'import hetsoc' 2>/dev/null; then
    err "the 'hetsoc' package is not importable."
    err "  The framework lives in host/hetsoc/ and is installed with"
    err "  'pip install -e host/'. If host/ is still empty, the framework area"
    err "  has not landed yet — there is nothing to test."
    err "  fix: make deps"
    exit 1
fi

# `import hetsoc` succeeding is NOT the same as hetsoc being installed. With
# host/ on PYTHONPATH, a directory with no __init__.py imports fine as an
# implicit namespace package — and then every `from hetsoc import X` fails with
# a baffling "cannot import name 'X' from 'hetsoc' (unknown location)". Catch
# that here, where it can be named, rather than in a conftest traceback.
if [ "$("${PY}" -c 'import hetsoc; print(hetsoc.__file__ or "")' 2>/dev/null)" = "" ]; then
    err "'hetsoc' resolves as a NAMESPACE package (hetsoc.__file__ is None)."
    err "  host/hetsoc/__init__.py is missing, so submodule imports will fail"
    err "  with 'cannot import name ... from hetsoc (unknown location)'."
    err "  The framework area owns host/hetsoc/ — this is not a test failure."
    exit 1
fi

TESTS_DIR="${HETSOC_ROOT}/tests"
if [ ! -d "${TESTS_DIR}" ] || ! compgen -G "${TESTS_DIR}/test_*.py" >/dev/null; then
    err "no tests found in ${TESTS_DIR} (expected tests/test_l<N>_<area>.py)."
    err "  The test suite is owned by the tests/ area and has not landed yet."
    err "  Nothing to run — this is not a test failure."
    exit 1
fi

# --- run ---------------------------------------------------------------------
mkdir -p "${HETSOC_RESULTS}"
JUNIT="${HETSOC_RESULTS}/pytest-${LEVEL}.xml"

hr
log "${LABEL}"
log "marker: ${MARKER}"
if [ "${#LEVEL_OPTS[@]}" -gt 0 ]; then
    log "opts:   ${LEVEL_OPTS[*]}"
fi
log "junit:  ${JUNIT}"
hr

set +e
"${PY}" -m pytest "${TESTS_DIR}" \
    -m "${MARKER}" \
    -ra -v \
    --junitxml="${JUNIT}" \
    "${LEVEL_OPTS[@]+"${LEVEL_OPTS[@]}"}" \
    "${EXTRA[@]+"${EXTRA[@]}"}"
RC=$?
set -e

# Translate pytest's exit codes into something an operator can act on. The
# distinction that matters is COLLECTION failed vs TESTS failed: a conftest
# that will not import is a broken checkout, not a regression in the design,
# and telling those apart is the difference between "go and look at the board"
# and "go and look at git".
case "${RC}" in
    0) ok "${LEVEL} passed" ;;
    5)
        # No test matched the marker. For a level whose cases have not been
        # written yet that is an empty result, not a regression — otherwise a
        # partially-populated suite red-lights CI for no reason.
        warn "no tests matched '${MARKER}' — level ${LEVEL} has no cases yet (empty, not failed)"
        RC=0
        ;;
    1) err "${LEVEL} FAILED — tests ran and some failed. See ${JUNIT}" ;;
    2)
        # pytest folds three different things into rc=2: Ctrl-C, a session-level
        # abort (conftest's wedge guard does this), and "Interrupted: N error
        # during collection". Say so rather than guessing.
        err "${LEVEL} INTERRUPTED (pytest rc=2). One of:"
        err "    * an error during COLLECTION — an import in tests/ or"
        err "      host/hetsoc/ is broken; grep the log above for 'ERROR tests/'"
        err "    * the suite's own wedge guard aborted the session after a"
        err "      hardware test failed (that is the guard working)"
        err "    * Ctrl-C"
        ;;
    3|4)
        err "${LEVEL} could not be COLLECTED (pytest rc=${RC}) — nothing ran."
        err "  This is an import/usage error in tests/ or host/hetsoc/, not a"
        err "  test result and not a hardware problem. Usual causes:"
        err "    * host/hetsoc/ is missing a module that tests/conftest.py imports"
        err "    * host/ is not installed:  make deps"
        err "    * a syntax error in a test or conftest:  make lint"
        ;;
    *) err "${LEVEL} FAILED (pytest rc=${RC}) — see ${JUNIT}" ;;
esac
exit "${RC}"
