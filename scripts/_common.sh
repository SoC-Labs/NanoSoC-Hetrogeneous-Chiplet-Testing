#!/usr/bin/env bash
# shellcheck shell=bash
#-----------------------------------------------------------------------------
# scripts/_common.sh — shared helpers. Sourced, never executed.
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
# Every script in scripts/ and flows/ starts with:
#
#     set -euo pipefail
#     . "$(dirname "$(readlink -f "$0")")/_common.sh"      # scripts/
#     . "$(dirname "$(readlink -f "$0")")/../scripts/_common.sh"   # flows/
#
# It does NOT set -e itself: the caller owns its own error discipline, and a
# sourced file that silently turns on errexit is how you get a script that dies
# three functions away from the line that actually failed.
#
# All environment defaults live in ONE place — set_env.sh — which this sources
# with stdout muted. That keeps `source set_env.sh && make ...` and a bare
# `./scripts/preflight.sh` on an unconfigured shell behaving identically.
#-----------------------------------------------------------------------------

HETSOC_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HETSOC_ROOT="${HETSOC_ROOT:-$(cd "${HETSOC_SCRIPT_DIR}/.." && pwd)}"
export HETSOC_ROOT

# Idempotent; stderr (the missing-submodule warning) deliberately survives.
# shellcheck source=../set_env.sh
. "${HETSOC_ROOT}/set_env.sh" >/dev/null

# --- Logging -----------------------------------------------------------------
# Colour only on a tty, so CI logs and `... | tee` stay greppable.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
    C_BLD=$'\033[1m';  C_OFF=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YEL=""; C_BLD=""; C_OFF=""
fi

log()  { printf '%s[hetsoc]%s %s\n' "${C_BLD}" "${C_OFF}" "$*"; }
ok()   { printf '%s[ OK ]%s %s\n'   "${C_GRN}" "${C_OFF}" "$*"; }
warn() { printf '%s[WARN]%s %s\n'   "${C_YEL}" "${C_OFF}" "$*" >&2; }
err()  { printf '%s[FAIL]%s %s\n'   "${C_RED}" "${C_OFF}" "$*" >&2; }
die()  { err "$*"; exit 1; }

hr() { printf '%.0s-' {1..76}; echo; }

# The banner every destructive/attended flow prints. Loud on purpose.
wedge_banner() {
    printf '%s' "${C_RED}${C_BLD}"
    cat <<'BANNER'
######################################################################
#                                                                    #
#   ATTENDED-ONLY FLOW — THIS CAN WEDGE THE SILICON                  #
#                                                                    #
#   The cross-die data plane intermittently hangs on current         #
#   silicon (recovery-stripped AXI FCSMs: a bit error has no         #
#   recovery path, so the link stops and the next PS access to the   #
#   peer aperture hangs the ZynqMP AXI bus with NO timeout).         #
#                                                                    #
#   A wedged KR260 is 100% packet loss and needs a JTAG POR from     #
#   ANOTHER host to come back:   make bench-recover BOARD=kr260_01   #
#                                                                    #
#   Do not start this and walk away. Do not run it in CI.            #
#                                                                    #
######################################################################
BANNER
    printf '%s' "${C_OFF}"
}

# --- Tooling -----------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

# The interpreter every python entry point should use: the venv if it exists,
# otherwise the best system python set_env.sh found. Never a bare `python`.
hetsoc_python() {
    if [ -x "${HETSOC_VENV}/bin/python" ]; then
        printf '%s\n' "${HETSOC_VENV}/bin/python"
    else
        printf '%s\n' "${HETSOC_PYTHON}"
    fi
}

# Read a field out of `fpgahub status --json` without needing jq (jq is NOT
# installed on this dev host). Usage: fpgahub_field <board> <key>
# Prints nothing and returns 1 if fpgahub or the board is unavailable.
fpgahub_field() {
    local board="$1" key="$2" py
    py="$(hetsoc_python)"
    have fpgahub || return 1
    fpgahub status --json 2>/dev/null | "${py}" -c '
import json, sys
board, key = sys.argv[1], sys.argv[2]
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for b in doc.get("boards", []):
    if b.get("name") == board:
        v = b.get(key)
        print("" if v is None else v)
        sys.exit(0)
sys.exit(1)
' "${board}" "${key}"
}

# --- SSH ---------------------------------------------------------------------
# BatchMode by default: an ssh that stops to ask for a password is a hang, and a
# hang in a bench script is indistinguishable from a wedged board. Password auth
# is opt-in via HETSOC_PASSWORD + sshpass, mirroring kr260_deploy.sh.
hetsoc_ssh_opts() {
    local opts=(-o StrictHostKeyChecking=no
                -o "ConnectTimeout=${HETSOC_SSH_TIMEOUT}"
                -o ServerAliveInterval=15
                -o ServerAliveCountMax=3)
    # Note: `if`, not `[ ... ] && ...` — under the caller's `set -e` a failed
    # test at the end of an AND-list aborts the whole script.
    if [ -z "${HETSOC_PASSWORD:-}" ]; then
        opts+=(-o BatchMode=yes)
    fi
    if [ -n "${HETSOC_SSH_PROXY:-}" ]; then
        opts+=(-o "ProxyJump=${HETSOC_SSH_PROXY}")
    fi
    printf '%s\n' "${opts[@]}"
}

# hetsoc_ssh <host> <command...>
hetsoc_ssh() {
    local host="$1"; shift
    local -a opts
    mapfile -t opts < <(hetsoc_ssh_opts)
    # SC2029: yes, "$@" is expanded on THIS side before ssh sees it. That is
    # the contract — callers pass a fully-formed remote command string (the
    # KR260_* env vars they need are set locally and interpolated here), the
    # same way tidelink's kr260_eth_run.sh does it.
    # shellcheck disable=SC2029
    if [ -n "${HETSOC_PASSWORD:-}" ] && have sshpass; then
        SSHPASS="${HETSOC_PASSWORD}" sshpass -e ssh "${opts[@]}" "${host}" "$@"
    else
        ssh "${opts[@]}" "${host}" "$@"
    fi
}

# Raw TCP reach test on a port, without ssh in the way. bash's /dev/tcp gives a
# clean up/down answer for "is there an sshd listening", which ssh's exit code
# does not: ssh returns 255 for a dead network AND for a rejected key.
tcp_probe() {
    local hostport="$1" port="${2:-22}" host
    host="${hostport#*@}"
    timeout 5 bash -c "exec 3<>/dev/tcp/${host}/${port}" 2>/dev/null
}

# Classify a board's reachability. Prints exactly one of:
#
#   ok           ssh works — the board is up and we can drive it
#   auth         TCP/sshd is up, our credentials are not accepted
#   hostkey      TCP/sshd is up, known_hosts disagrees
#   degraded     TCP:22 is open but ssh failed for some other reason
#   unreachable  nothing is listening — powered off, or WEDGED
#
# THIS DISTINCTION IS A SAFETY PROPERTY, NOT A NICETY. `ssh ... true` exits 255
# for a wedged board and for a missing key alike, and treating the second as
# the first sends an operator to JTAG-POR a perfectly healthy board that
# somebody else may be using. Observed 2026-07-29: kr260_02 answered ping
# normally while ssh returned "Permission denied (publickey,password)".
#
# Only `unreachable` may ever be read as "this board might be wedged".
board_probe() {
    local host="$1" out rc=0
    # `|| rc=$?`, not `; rc=$?`. A bare assignment from a failing command
    # substitution IS a failing simple command, so under the caller's `set -e`
    # it aborts the script before this function can classify anything — the
    # exact bug this whole function exists to prevent.
    out="$(hetsoc_ssh "${host}" true 2>&1)" || rc=$?
    if [ "${rc}" -eq 0 ]; then printf 'ok\n'; return 0; fi

    case "${out}" in
        *"Permission denied"*|*"Too many authentication failures"*|\
        *"Authentication failed"*|*"Password:"*)
            printf 'auth\n'; return 0 ;;
        *"Host key verification failed"*|*"REMOTE HOST IDENTIFICATION HAS CHANGED"*)
            printf 'hostkey\n'; return 0 ;;
    esac

    if tcp_probe "${host}" 22; then
        printf 'degraded\n'
    else
        printf 'unreachable\n'
    fi
    # ALWAYS exits 0: the verdict is the STDOUT string, not the status. A
    # non-zero return here would abort any caller running under `set -e` at
    # `verdict="$(board_probe ...)"` — which is every one of them.
    return 0
}

# True only when we can actually drive the board. Everything that needs to
# reason about WHY should call board_probe instead.
board_reachable() {
    [ "$(board_probe "$1")" = "ok" ]
}

# One-line human explanation for a board_probe verdict.
probe_detail() {
    case "$1" in
        ok)          printf 'ssh OK\n' ;;
        auth)        printf 'board is UP (sshd answering) but ssh auth was refused — add your key, or set HETSOC_PASSWORD in site.local.sh. NOT a wedge: do not POR it.\n' ;;
        hostkey)     printf 'board is UP but known_hosts disagrees — likely re-imaged. ssh-keygen -R the host.\n' ;;
        degraded)    printf 'TCP:22 is open but ssh will not complete — board is alive; check sshd/config.\n' ;;
        unreachable) printf 'nothing listening on :22 — powered off, or WEDGED (a wedged KR260 goes to 100%% packet loss).\n' ;;
        *)           printf '%s\n' "$1" ;;
    esac
}

# --- Bench identity ----------------------------------------------------------
# Resolve a board's ssh endpoint. fpgahub is authoritative when reachable
# (host_ssh is what the daemon believes); the set_env.sh default is the offline
# fallback so a script still works when fpgahubd is down.
board_host() {
    local board="$1" from_hub=""
    from_hub="$(fpgahub_field "${board}" host_ssh 2>/dev/null || true)"
    if [ -n "${from_hub}" ]; then
        printf '%s\n' "${from_hub}"
        return 0
    fi
    case "${board}" in
        "${HETSOC_BOARD_A}") printf '%s\n' "${HETSOC_BOARD_A_HOST}" ;;
        "${HETSOC_BOARD_B}") printf '%s\n' "${HETSOC_BOARD_B_HOST}" ;;
        *) return 1 ;;
    esac
}

# The L4/L5 gate, enforced in one place so no caller can forget it.
require_wedge_optin() {
    local what="${1:-this flow}"
    if [ "${I_ACCEPT_WEDGE_RISK:-0}" != "1" ]; then
        wedge_banner
        err "${what} is attended-only and refused by default."
        err "Re-run with I_ACCEPT_WEDGE_RISK=1 once you are sat at the bench"
        err "and able to POR a wedged board. See docs/SAFETY.md."
        exit 2
    fi
    wedge_banner
    log "I_ACCEPT_WEDGE_RISK=1 — proceeding. You are the recovery plan."
}
