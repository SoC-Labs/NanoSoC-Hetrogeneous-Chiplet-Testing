#!/usr/bin/env bash
#-----------------------------------------------------------------------------
# scripts/regress.sh — the full regression, one command, one pass/fail table.
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
#   ./scripts/regress.sh                  lint + L0 + L1/L2 + L3      (safe)
#   ./scripts/regress.sh --offline        lint + L0 only              (CI)
#   ./scripts/regress.sh --deploy         reflash both dies first, then as above
#   ./scripts/regress.sh --data-plane     + L4      ATTENDED, needs I_ACCEPT_WEDGE_RISK=1
#   ./scripts/regress.sh --soak           + L4 + L5 ATTENDED, needs I_ACCEPT_WEDGE_RISK=1
#
# ORDERED CHEAPEST-FIRST so a broken tree surfaces in seconds, not minutes, and
# so nothing ever touches a board before the pure-host logic is known good:
#
#   1. lint          seconds   ruff + shellcheck
#   2. L0 offline    seconds   address maths, registry, guards — no boards
#   3. preflight     seconds   are both boards there and lease-free?
#   4. L1/L2 single  ~1 min    read-only probes, then config-plane writes
#   5. L3 pair       minutes   link bring-up + cross-die control plane
#   6. L4 data plane ATTENDED  cross-die transfers  *** can wedge silicon ***
#   7. L5 soak       ATTENDED  stress                *** can wedge silicon ***
#
# Steps 1-5 are the DEFAULT and are CI-safe. 6 and 7 are opt-in twice over: a
# flag AND I_ACCEPT_WEDGE_RISK=1. That mirrors the eth-chiplet's own
# kr260_eth_regress.py, whose default suite is deliberately die-local because
# the cross-die data plane intermittently hangs on current silicon.
#
# UNLIKE the per-level targets, this does NOT stop at the first failure (no
# `set -e` around the steps): one run should tell you the whole story. A level
# that cannot run because the one before it failed is reported SKIP, not FAIL —
# there is no information in "L3 failed because L0 failed".
#
# Exit code is non-zero iff a gating step failed.
#-----------------------------------------------------------------------------
set -uo pipefail
# shellcheck source=_common.sh
. "$(dirname "$(readlink -f "$0")")/_common.sh"

DO_DEPLOY=0
DO_HW=1
DO_DATA=0
DO_SOAK=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --offline)    DO_HW=0 ;;
        --deploy)     DO_DEPLOY=1 ;;
        --data-plane) DO_DATA=1 ;;
        --soak)       DO_DATA=1; DO_SOAK=1 ;;
        -h|--help)    sed -n '2,40p' "$0"; exit 0 ;;
        *)            err "unknown option '$1'"; exit 2 ;;
    esac
    shift
done

mkdir -p "${HETSOC_RESULTS}"
LOGDIR="${HETSOC_BUILD}/regress"
mkdir -p "${LOGDIR}"

declare -a ROWS
FAILED=0
pass_row() { ROWS+=("$1|PASS|$2"); }
fail_row() { ROWS+=("$1|FAIL|$2"); FAILED=1; }
skip_row() { ROWS+=("$1|SKIP|$2"); }

# run_step NAME GATING CMD...
# GATING=1 -> a failure fails the regression; 0 -> reported but non-fatal.
run_step() {
    local name="$1" gating="$2"; shift 2
    local logf="${LOGDIR}/${name}.log"
    hr
    log ">> ${name}"
    "$@" 2>&1 | tee "${logf}"
    local rc="${PIPESTATUS[0]}"
    if [ "${rc}" -eq 0 ]; then
        pass_row "${name}" "rc=0 (${logf})"
        return 0
    fi
    if [ "${gating}" -eq 1 ]; then
        fail_row "${name}" "rc=${rc} (see ${logf})"
    else
        ROWS+=("${name}|WARN|rc=${rc}, non-gating (see ${logf})")
    fi
    return "${rc}"
}

hr
log "hetsoc regression   logs: ${LOGDIR}   results: ${HETSOC_RESULTS}"
log "scope: lint + L0$([ "${DO_HW}" -eq 1 ] && echo " + L1/L2 + L3")$([ "${DO_DATA}" -eq 1 ] && echo " + L4")$([ "${DO_SOAK}" -eq 1 ] && echo " + L5")"
hr

# --- 1. static ---------------------------------------------------------------
run_step lint 1 "${HETSOC_SCRIPT_DIR}/lint.sh"

# --- 2. L0 -------------------------------------------------------------------
run_step l0_offline 1 "${HETSOC_SCRIPT_DIR}/run_pytest.sh" --level l0
L0_OK=$?

# --- 3..7 hardware -----------------------------------------------------------
if [ "${DO_HW}" -eq 0 ]; then
    skip_row preflight   "--offline"
    skip_row l1l2_single "--offline"
    skip_row l3_pair     "--offline"
    skip_row l4_dataplane "--offline"
    skip_row l5_soak     "--offline"
elif [ "${L0_OK}" -ne 0 ]; then
    # Refusing to go near a board when the host-side guards are broken is the
    # whole point of L0: hetsoc.safety is what stops an out-of-window address
    # reaching /dev/mem, and an out-of-window read is what wedges the PS.
    warn "L0 failed — the address guards are not trustworthy. NOT touching the bench."
    skip_row preflight   "L0 failed"
    skip_row l1l2_single "L0 failed"
    skip_row l3_pair     "L0 failed"
    skip_row l4_dataplane "L0 failed"
    skip_row l5_soak     "L0 failed"
else
    run_step preflight 1 "${HETSOC_SCRIPT_DIR}/preflight.sh" --pair
    PRE_OK=$?

    if [ "${PRE_OK}" -ne 0 ]; then
        warn "preflight failed — the bench is not ready. Skipping every board step."
        skip_row l1l2_single "preflight failed"
        skip_row l3_pair     "preflight failed"
        skip_row l4_dataplane "preflight failed"
        skip_row l5_soak     "preflight failed"
    else
        if [ "${DO_DEPLOY}" -eq 1 ]; then
            run_step deploy_pair 1 "${HETSOC_ROOT}/flows/deploy_pair.sh"
        else
            skip_row deploy_pair "not requested (--deploy)"
        fi

        run_step l1l2_single 1 "${HETSOC_SCRIPT_DIR}/run_pytest.sh" --level l1l2
        run_step l3_pair     1 "${HETSOC_SCRIPT_DIR}/run_pytest.sh" --level l3
        L3_OK=$?

        if [ "${DO_DATA}" -eq 1 ]; then
            if [ "${L3_OK}" -ne 0 ]; then
                skip_row l4_dataplane "L3 failed — link/control plane not healthy"
            else
                # Non-gating on purpose: on current silicon an L4 failure is
                # expected often enough that it must not red-light the run. It
                # is a characterisation datapoint, not a regression signal,
                # until the FCSM recovery fix lands.
                run_step l4_dataplane 0 "${HETSOC_SCRIPT_DIR}/run_pytest.sh" --level l4
            fi
        else
            skip_row l4_dataplane "not requested (--data-plane; ATTENDED ONLY)"
        fi

        if [ "${DO_SOAK}" -eq 1 ]; then
            run_step l5_soak 0 "${HETSOC_SCRIPT_DIR}/run_pytest.sh" --level l5
        else
            skip_row l5_soak "not requested (--soak; ATTENDED ONLY)"
        fi
    fi
fi

# --- publish -----------------------------------------------------------------
"${HETSOC_SCRIPT_DIR}/py.sh" "${HETSOC_ROOT}/ci/results_to_junit.py" \
    "${HETSOC_RESULTS}" -o "${HETSOC_RESULTS}/junit.xml" >/dev/null 2>&1 \
    || warn "JUnit merge failed (non-fatal)"

echo
hr
printf '  %-16s %-6s %s\n' "STEP" "RESULT" "DETAIL"
hr
for r in "${ROWS[@]}"; do
    IFS='|' read -r n v d <<<"${r}"
    printf '  %-16s %-6s %s\n' "${n}" "${v}" "${d}"
done
hr
if [ "${FAILED}" -eq 0 ]; then
    ok "regression PASS"
else
    err "regression FAIL — see the table above and ${LOGDIR}/*.log"
fi
exit "${FAILED}"
