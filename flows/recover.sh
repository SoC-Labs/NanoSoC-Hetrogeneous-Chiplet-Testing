#!/usr/bin/env bash
#-----------------------------------------------------------------------------
# flows/recover.sh — bring a wedged KR260 back. JTAG POR via fpgahub.
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
#   ./flows/recover.sh                       POR both boards (the pair)
#   ./flows/recover.sh --board kr260_01      POR one board
#   ./flows/recover.sh --check               diagnose only, reset nothing
#
# WHEN YOU NEED THIS
# ------------------
# Any PS read of a PL address the chiplet SoC does not decode hangs the ZynqMP
# AXI bus with NO timeout. The board goes to 100% packet loss: ssh dies, ping
# dies, the console is dead. Nothing software-side recovers it — `sudo reboot`
# makes it worse (the PMUFW reset wedges too). Only a power-on reset over JTAG
# brings it back, and that has to be issued by the host holding the JTAG cable.
#
# THE TWO TRAPS THIS SCRIPT EXISTS TO ROUTE AROUND
# ------------------------------------------------
# 1. WRONG HOST (the "404 workaround"). fpgahub's per-board endpoints
#    (board/target reset, board show, actions run) return HTTP 404 from this
#    dev box. The cause is a CLI/daemon version skew: the CLI installed here is
#    newer than the daemon on mapstone-dev and calls routes that daemon does
#    not serve. Collection endpoints (`status`, `lease acquire/release`) are
#    common to both and DO work locally. So: reset is dispatched over ssh to
#    mapstone-dev, where the CLI matches its own daemon.
#
# 2. GROUP RESET. On mapstone-dev's (older) CLI, `fpgahub board <name>` means
#    the whole GROUP — it resets every member sequentially and stops at the
#    first failure. Since the LAN8720 topology added a `kr260_0N_pl` member
#    that has no reset method at all ("HTTP 400: no reset method 'default'"),
#    `board reset kr260_01` fails before the real board is ever POR'd. The
#    single member is addressed with `target reset`, which is what we call.
#
#    Verified 2026-07-29:
#      ssh mapstone-dev 'fpgahub target reset kr260_01 --list'
#        default -> kr260_jtag_por  "POR over JTAG — the only reliable KR260
#                                    recovery (a soft reset wedges the PMU)"
#        reboot  -> kr260_kexec_reboot
#    `--method reboot` (kexec) is the gentler everyday path, offered by --soft;
#    it is NOT enough for an AXI-wedged board.
#
# If ssh to mapstone-dev is itself unavailable, the script prints the raw
# unix-socket API call to run there by hand. It never leaves you without a next
# step — being stuck with a dead board and no instructions is the failure mode
# this whole file is written against.
#-----------------------------------------------------------------------------
set -uo pipefail
# shellcheck source=../scripts/_common.sh
. "$(dirname "$(readlink -f "$0")")/../scripts/_common.sh"

METHOD=default
CHECK_ONLY=0
FORCE_POR=0
declare -a TARGETS=()

while [ "$#" -gt 0 ]; do
    case "$1" in
        --board)     TARGETS+=("${2:-}"); shift 2 ;;
        --soft)      METHOD=reboot; shift ;;
        --check)     CHECK_ONLY=1; shift ;;
        --force-por) FORCE_POR=1; shift ;;
        -h|--help)   sed -n '2,48p' "$0"; exit 0 ;;
        *)           err "unknown option '$1'"; exit 2 ;;
    esac
done
if [ "${#TARGETS[@]}" -eq 0 ]; then
    TARGETS=("${HETSOC_BOARD_A}" "${HETSOC_BOARD_B}")
fi

DEV_HOST="${HETSOC_FPGAHUB_DEV_HOST}"

manual_instructions() {
    local board="$1"
    cat >&2 <<EOF

  Run this by hand on ${DEV_HOST}:

      fpgahub target reset ${board} --method ${METHOD} --yes

  or, if that CLI is also skewed, straight at the daemon socket:

      curl -s --unix-socket /run/fpgahub/fpgahub.sock \\
           -X POST 'http://localhost/api/v1/targets/${board}/reset' \\
           -H 'Content-Type: application/json' \\
           -d '{"method":"${METHOD}","confirm":true}'

  Expect plugin=kr260_jtag_por and the board back in ~40 s.
  Do NOT use 'fpgahub board reset ${board}' — that is the group reset, and it
  dies on the ${board}_pl member before POR'ing anything.
EOF
}

# --- diagnose ----------------------------------------------------------------
hr
log "recovery check for: ${TARGETS[*]}"
hr

# A POR is destructive: it drops the PL, kills anything running, and takes the
# board out for ~40s. So the bar for issuing one is "nothing is listening on
# :22", NOT "ssh failed". Those are different things — an ssh that is refused
# for want of a key comes back 255 exactly like a dead board does, and POR'ing
# a healthy board somebody else is using is the worst outcome this script has.
# See board_probe() in scripts/_common.sh.
declare -a DEAD=()
declare -a ALIVE_BUT_STUCK=()
for b in "${TARGETS[@]}"; do
    host="$(board_host "${b}" 2>/dev/null || true)"
    if [ -z "${host}" ]; then
        if [ "${FORCE_POR}" -eq 1 ]; then
            warn "${b}: no ssh endpoint known — POR'ing anyway (--force-por)"
            DEAD+=("${b}")
        else
            warn "${b}: no ssh endpoint known — cannot tell wedged from unconfigured."
            warn "     REFUSING to POR blind. Set HETSOC_BOARD_*_HOST, or pass --force-por."
        fi
        continue
    fi
    verdict="$(board_probe "${host}")"
    case "${verdict}" in
        ok)
            ok "${b} (${host}) is answering ssh — NOT wedged, leaving it alone" ;;
        unreachable)
            err "${b} (${host}) — $(probe_detail "${verdict}")"
            DEAD+=("${b}") ;;
        *)
            warn "${b} (${host}) — $(probe_detail "${verdict}")"
            warn "     The board is ALIVE. A POR would not fix this and would"
            warn "     disrupt whoever is using it. Skipping."
            ALIVE_BUT_STUCK+=("${b}") ;;
    esac
done

if [ "${#DEAD[@]}" -eq 0 ]; then
    if [ "${#ALIVE_BUT_STUCK[@]}" -gt 0 ]; then
        warn "nothing to recover: ${ALIVE_BUT_STUCK[*]} are up but not driveable (an access problem, not a wedge)"
        exit 1
    fi
    ok "nothing to recover — both boards are alive"
    exit 0
fi

if [ "${CHECK_ONLY}" -eq 1 ]; then
    warn "--check: would POR ${DEAD[*]} (method=${METHOD}). Nothing done."
    exit 0
fi

# --- reset -------------------------------------------------------------------
if ! hetsoc_ssh "${DEV_HOST}" true >/dev/null 2>&1; then
    err "cannot ssh to ${DEV_HOST} — that is where the JTAG cables and the"
    err "matching fpgahub daemon live, so the POR cannot be dispatched from here."
    for b in "${DEAD[@]}"; do manual_instructions "${b}"; done
    exit 1
fi

RC=0
for b in "${DEAD[@]}"; do
    hr
    log "POR ${b} via ${DEV_HOST} (fpgahub target reset --method ${METHOD})"
    if hetsoc_ssh "${DEV_HOST}" "fpgahub target reset ${b} --method ${METHOD} --yes"; then
        ok "${b}: reset dispatched"
    else
        err "${b}: reset FAILED"
        manual_instructions "${b}"
        RC=1
    fi
done

# A POR takes ~40 s to come back. Poll rather than sleeping blind, so the
# common case returns as soon as the board answers.
hr
log "waiting for boards to come back (up to 120s)"
deadline=$(( $(date +%s) + 120 ))
for b in "${DEAD[@]}"; do
    host="$(board_host "${b}" 2>/dev/null || true)"
    [ -n "${host}" ] || continue
    while :; do
        if board_reachable "${host}"; then
            ok "${b} is back (${host})"
            break
        fi
        if [ "$(date +%s)" -ge "${deadline}" ]; then
            err "${b} did not come back within the window — check the bench physically"
            RC=1
            break
        fi
        sleep 5
    done
done

hr
if [ "${RC}" -eq 0 ]; then
    ok "recovery complete."
    log "The PL is now UNPROGRAMMED. Re-deploy before testing:  make deploy-pair"
else
    err "recovery incomplete — see above"
fi
exit "${RC}"
