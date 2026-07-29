#!/usr/bin/env bash
#-----------------------------------------------------------------------------
# flows/bench_status.sh — what is the bench actually doing right now?
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
#   ./flows/bench_status.sh          (make bench-status)
#
# STRICTLY READ-ONLY. It leases nothing, deploys nothing, writes no register.
# The heaviest thing it does on a board is the eth_ss_0 boot-ROM aliveness
# probe plus a TideLink config-plane read — RO/combinational addresses only,
# inside the decoded backdoor window. It cannot wedge anything, which is why it
# is the FIRST thing to run when you sit down at the bench and the first thing
# to run when something looks wrong.
#
# It deliberately does NOT hard-fail on a dark board: "board B is not answering"
# is the answer you came for, not an error. Exit is non-zero only if neither
# board is reachable at all.
#-----------------------------------------------------------------------------
set -uo pipefail
# shellcheck source=../scripts/_common.sh
. "$(dirname "$(readlink -f "$0")")/../scripts/_common.sh"

PY="$(hetsoc_python)"
RS="${HETSOC_TL_SCRIPTS}/kr260_eth_run.sh"

hr
log "bench status — read-only. Nothing below writes to a board."
hr

# --- 1. fpgahub view ---------------------------------------------------------
echo
log "fpgahub"
if have fpgahub && fpgahub health >/dev/null 2>&1; then
    for b in "${HETSOC_BOARD_A}" "${HETSOC_BOARD_B}"; do
        printf '  %-12s in_use=%-4s lease=%-8s holder=%-20s ssh=%s\n' \
            "${b}" \
            "$(fpgahub_field "${b}" in_use        2>/dev/null || echo '?')" \
            "$(fpgahub_field "${b}" lease_state   2>/dev/null || echo '?')" \
            "$(fpgahub_field "${b}" lease_holder  2>/dev/null || echo '-')" \
            "$(fpgahub_field "${b}" host_ssh      2>/dev/null || echo '-')"
    done
else
    warn "  fpgahub daemon not reachable — falling back to set_env.sh defaults"
fi

# --- 2. reachability ---------------------------------------------------------
echo
log "ssh reachability"
ALIVE=0
declare -A HOSTS=()
declare -A VERDICTS=()
for b in "${HETSOC_BOARD_A}" "${HETSOC_BOARD_B}"; do
    host="$(board_host "${b}" 2>/dev/null || true)"
    HOSTS["${b}"]="${host}"
    if [ -z "${host}" ]; then
        err "  ${b}: no ssh endpoint"
        VERDICTS["${b}"]="no-endpoint"
        continue
    fi
    verdict="$(board_probe "${host}")"
    VERDICTS["${b}"]="${verdict}"
    if [ "${verdict}" = "ok" ]; then
        ok "  ${b}: ${host}"
        ALIVE=$((ALIVE + 1))
    elif [ "${verdict}" = "unreachable" ]; then
        err "  ${b}: ${host} — $(probe_detail "${verdict}")"
    else
        # Alive, just not driveable from here. Do not imply a wedge.
        warn "  ${b}: ${host} — $(probe_detail "${verdict}")"
    fi
done

if [ "${ALIVE}" -eq 0 ]; then
    hr
    err "neither board is reachable. Nothing further to probe."
    exit 1
fi

# --- 3. the SoC itself -------------------------------------------------------
echo
log "chiplet SoC / TideLink state"

if "${PY}" -c 'import hetsoc' 2>/dev/null; then
    "${PY}" - <<'PY'
# The framework's own read-only view. Every access below goes through
# Target.to_host(), so an out-of-window address raises rather than wedging.
import sys
try:
    from hetsoc.config import load
    from hetsoc.board import Board
    cfg = load()
    boards = cfg["board"] if isinstance(cfg, dict) else {}
    for name, spec in boards.items():
        try:
            b = Board(**spec) if isinstance(spec, dict) else spec
            alive = b.alive()
            st = b.lane_status() if alive else None
            print("  %-10s alive=%-5s %s" % (
                name, alive,
                ("fcsm=%s cal_done=%s link_up=%s" % (st.fcsm, st.cal_done, st.link_up))
                if st is not None else "(not probed)"))
        except Exception as exc:                      # noqa: BLE001
            print("  %-10s ERROR %s: %s" % (name, type(exc).__name__, exc))
except Exception as exc:                              # noqa: BLE001
    print("  hetsoc present but not usable yet: %s: %s" % (type(exc).__name__, exc))
    sys.exit(0)
PY
elif [ -f "${RS}" ]; then
    warn "  hetsoc not importable — using TideLink's kr260_eth_run.sh status (read-only)"
    for b in "${HETSOC_BOARD_A}" "${HETSOC_BOARD_B}"; do
        host="${HOSTS[${b}]}"
        [ -n "${host}" ] || continue
        # Only probe boards we can actually log into; running the SoC probe
        # against a board we cannot ssh to just prints ssh errors.
        [ "${VERDICTS[${b}]}" = "ok" ] || continue
        echo "  --- ${b} (${host}) ---"
        KR260_HOST="${host}" KR260_PASSWORD="${HETSOC_PASSWORD:-}" \
            bash "${RS}" status 2>&1 | sed 's/^/    /' || true
    done
else
    warn "  neither hetsoc nor ${RS} available — run 'make deps'"
fi

hr
ok "bench status complete (nothing was written)"
