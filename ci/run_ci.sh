#!/usr/bin/env bash
#-----------------------------------------------------------------------------
# ci/run_ci.sh — the single entry point CI invokes. Nothing else.
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
#   ci/run_ci.sh offline      lint + L0.        Hosted runners. Every push.
#   ci/run_ci.sh nightly      + L1/L2 + L3.     SELF-HOSTED runner only.
#   ci/run_ci.sh lint         lint only.
#
# There is deliberately NO mode that runs L4 or L5. The cross-die data plane
# intermittently hangs on current silicon and a hang is a wedge, and a wedge
# needs a human with access to a JTAG POR. An unattended job that can leave the
# shared bench dead until someone notices is not a CI job, it is an outage
# generator. Those levels are run by hand: see docs/CI.md.
#
# Exit code is the regression's. Artefacts land in build/results/ either way —
# junit.xml and dashboard.html are produced even on failure, because a failed
# run is exactly when you want them.
#-----------------------------------------------------------------------------
set -uo pipefail

CI_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ROOT="$(cd "${CI_DIR}/.." && pwd)"
cd "${ROOT}" || { echo "run_ci: cannot cd to ${ROOT}" >&2; exit 1; }

MODE="${1:-offline}"

echo "== hetsoc CI: mode=${MODE} =="
echo "   root:   ${ROOT}"
echo "   commit: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "   runner: $(hostname -s 2>/dev/null || echo unknown)"

# Belt and braces: even if a workflow is mis-edited to pass a hardware mode to a
# hosted runner, the opt-in gate is pinned off here, so run_pytest.sh refuses
# L4/L5 no matter how it is reached.
export I_ACCEPT_WEDGE_RISK=0

# SETUP DEPTH IS MODE-DEPENDENT, and that is deliberate:
#
#   offline/lint  -> `make venv` only. L0 imports nothing but hetsoc, so the
#                    submodules are dead weight — AND they are declared over
#                    SSH (git@github.com), which a hosted GitHub runner cannot
#                    clone without a deploy key. Requiring them would make the
#                    always-on gate depend on credentials it does not need.
#   nightly       -> `make deps`. The bench flows shell out to TideLink's
#                    kr260_eth_run.sh / deploy_pair_role, which live in the
#                    submodules. The self-hosted lab runner has the keys.
echo "== setup =="
case "${MODE}" in
    nightly) SETUP_TARGET=deps ;;
    *)       SETUP_TARGET=venv ;;
esac
if ! make "${SETUP_TARGET}"; then
    echo "== CI FAILED: 'make ${SETUP_TARGET}' could not prepare the environment ==" >&2
    exit 1
fi

RC=0
case "${MODE}" in
    lint)
        make lint || RC=$?
        ;;
    offline)
        ./scripts/regress.sh --offline || RC=$?
        ;;
    nightly)
        # Hardware levels. preflight inside regress.sh decides whether the bench
        # is actually there; on a runner with no boards this fails fast and
        # loudly rather than hanging.
        ./scripts/regress.sh || RC=$?
        ;;
    *)
        echo "usage: run_ci.sh offline|nightly|lint" >&2
        exit 2
        ;;
esac

echo "== publishing results =="
./scripts/py.sh ci/results_to_junit.py build/results -o build/results/junit.xml || true
./scripts/py.sh ci/dashboard.py build/results \
    -o build/results/dashboard.html --markdown build/results/summary.md || true

echo "== hetsoc CI: mode=${MODE} rc=${RC} =="
exit "${RC}"
