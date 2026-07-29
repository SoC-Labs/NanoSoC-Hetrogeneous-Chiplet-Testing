#!/usr/bin/env bash
#-----------------------------------------------------------------------------
# flows/bringup_pair.sh — bring the D2D link up on BOTH dies, concurrently.
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
#   ./flows/bringup_pair.sh              bring up (fresh dies only)
#   ./flows/bringup_pair.sh --verify     read-only: is the link already up?
#
# CONCURRENCY IS NOT AN OPTIMISATION HERE — IT IS THE PROTOCOL.
# Calibration completes only when both ends are training at once: cal_done on
# each die gates on the peer and the ribbon. Bring one side up, wait, then the
# other, and the first has already given up. The two runs self-synchronise, so
# they are launched together and joined.
#
# FRESH DIES ONLY.
# Bring-up drives the link-layer software reset (LL_SWRESET) on the way into
# data mode. On an ALREADY-LIVE link that resets one end under a running peer:
# the two desync and the sender's next peer write hangs -> PS bus wedge. This
# is not theoretical; it is what wedged die_a on 2026-07-29.
#
# So the rule, enforced below:
#   * link already up  -> refuse, and tell you to verify instead
#   * link down        -> bring up
#   * --verify         -> never writes anything
#
# `make deploy-pair` leaves both dies fresh, which is the state this wants.
#
# IMPLEMENTATION: prefers the hetsoc framework (ChipletPair.bringup(), which is
# the tested path and carries the timeout/wedge guards). Falls back to
# TideLink's kr260_eth_run.sh so the flow still works before host/hetsoc/ lands.
#-----------------------------------------------------------------------------
set -uo pipefail
# shellcheck source=../scripts/_common.sh
. "$(dirname "$(readlink -f "$0")")/../scripts/_common.sh"

VERIFY_ONLY=0
FORCE=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --verify)  VERIFY_ONLY=1; shift ;;
        --force)   FORCE=1; shift ;;
        -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
        *)         err "unknown option '$1'"; exit 2 ;;
    esac
done

PY="$(hetsoc_python)"
HAVE_HETSOC=0
if "${PY}" -c 'import hetsoc' 2>/dev/null; then HAVE_HETSOC=1; fi

#-----------------------------------------------------------------------------
# Path A — the framework (preferred)
#-----------------------------------------------------------------------------
via_hetsoc() {
    HETSOC_VERIFY_ONLY="${VERIFY_ONLY}" HETSOC_FORCE="${FORCE}" "${PY}" - <<'PY'
import os, sys

from hetsoc.config import load
from hetsoc.pair import ChipletPair
from hetsoc.board import Board

verify_only = os.environ.get("HETSOC_VERIFY_ONLY") == "1"
force = os.environ.get("HETSOC_FORCE") == "1"

cfg = load()
pair = cfg.pair() if hasattr(cfg, "pair") else None
if pair is None:
    # Fall back to constructing from the documented config shape.
    a = Board(**cfg["board"]["eth"])
    b = Board(**cfg["board"]["compute"])
    pair = ChipletPair(a, b)

up = pair.verify_link()
print("link currently: %s" % ("UP" if up else "DOWN"))

if verify_only:
    sys.exit(0 if up else 1)

if up and not force:
    print("REFUSING to re-run bring-up on a LIVE link — that desyncs it and")
    print("wedges the sender. The link is already up; nothing to do.")
    print("(--force only if you know both dies were just reflashed.)")
    sys.exit(0)

pair.bringup(deploy=False)
ok = pair.verify_link()
print("link after bring-up: %s" % ("UP" if ok else "DOWN"))
sys.exit(0 if ok else 1)
PY
}

#-----------------------------------------------------------------------------
# Path B — TideLink's own bench script, run on both boards at once
#-----------------------------------------------------------------------------
run_sh() { printf '%s\n' "${HETSOC_TL_SCRIPTS}/kr260_eth_run.sh"; }

via_tidelink() {
    local rs; rs="$(run_sh)"
    if [ ! -f "${rs}" ]; then
        err "neither the hetsoc package nor ${rs} is available."
        err "  run 'make deps' — the TideLink submodule under deps/eth-chiplet is missing."
        return 1
    fi

    local host_a host_b
    host_a="$(board_host "${HETSOC_BOARD_A}")" || return 1
    host_b="$(board_host "${HETSOC_BOARD_B}")" || return 1

    local mode="bringup"
    [ "${VERIFY_ONLY}" -eq 1 ] && mode="status"

    local out_a="${HETSOC_BUILD}/bringup_${HETSOC_ROLE_A}.log"
    local out_b="${HETSOC_BUILD}/bringup_${HETSOC_ROLE_B}.log"
    mkdir -p "${HETSOC_BUILD}"

    if [ "${mode}" = "bringup" ] && [ "${FORCE}" -eq 0 ]; then
        # Read-only pre-check: never start a write-mode bring-up on a live link.
        log "pre-check: is the link already up? (read-only)"
        if KR260_HOST="${host_a}" KR260_PASSWORD="${HETSOC_PASSWORD:-}" \
             bash "${rs}" status 2>&1 | grep -q 'LINK UP'; then
            warn "die_a already reports LINK UP."
            err  "REFUSING to bring up a live link — it desyncs and wedges the sender."
            err  "  verify instead:  ./flows/bringup_pair.sh --verify"
            err  "  or reflash first: make deploy-pair   (then bring up fresh dies)"
            return 2
        fi
    fi

    hr
    log "${mode} on BOTH dies concurrently (they gate on each other)"
    log "  ${HETSOC_ROLE_A}: ${HETSOC_BOARD_A} ${host_a} -> ${out_a}"
    log "  ${HETSOC_ROLE_B}: ${HETSOC_BOARD_B} ${host_b} -> ${out_b}"
    hr

    KR260_HOST="${host_a}" KR260_ETH_ROLE="${HETSOC_ROLE_A}" \
        KR260_PASSWORD="${HETSOC_PASSWORD:-}" bash "${rs}" "${mode}" >"${out_a}" 2>&1 &
    local pid_a=$!
    KR260_HOST="${host_b}" KR260_ETH_ROLE="${HETSOC_ROLE_B}" \
        KR260_PASSWORD="${HETSOC_PASSWORD:-}" bash "${rs}" "${mode}" >"${out_b}" 2>&1 &
    local pid_b=$!

    wait "${pid_a}"; local rc_a=$?
    wait "${pid_b}"; local rc_b=$?

    local up_a=0 up_b=0
    grep -q 'LINK UP' "${out_a}" && up_a=1
    grep -q 'LINK UP' "${out_b}" && up_b=1

    sed 's/^/  A| /' "${out_a}"
    sed 's/^/  B| /' "${out_b}"

    hr
    printf '  %-8s rc=%-3s link=%s\n' "${HETSOC_ROLE_A}" "${rc_a}" "$([ "${up_a}" -eq 1 ] && echo UP || echo DOWN)"
    printf '  %-8s rc=%-3s link=%s\n' "${HETSOC_ROLE_B}" "${rc_b}" "$([ "${up_b}" -eq 1 ] && echo UP || echo DOWN)"
    hr

    if [ "${up_a}" -eq 1 ] && [ "${up_b}" -eq 1 ]; then
        return 0
    fi
    return 1
}

if [ "${HAVE_HETSOC}" -eq 1 ]; then
    log "using the hetsoc framework"
    via_hetsoc
    RC=$?
    if [ "${RC}" -ne 0 ] && [ "${VERIFY_ONLY}" -eq 0 ]; then
        warn "hetsoc bring-up path returned ${RC}"
    fi
else
    warn "hetsoc is not importable yet — using TideLink's kr260_eth_run.sh directly"
    via_tidelink
    RC=$?
fi

if [ "${RC}" -eq 0 ]; then
    ok "link is UP on both dies (FCSM=4)."
    [ "${VERIFY_ONLY}" -eq 1 ] || log "Next: make test-pair   (L3, control plane, wedge-safe)"
else
    err "link is not up on both dies."
    err "  Both dies must be FRESH (just reflashed) for bring-up to work:"
    err "     make deploy-pair && make bench-bringup"
    err "  If a board has stopped answering entirely it is wedged:"
    err "     make bench-recover"
fi
exit "${RC}"
