#!/usr/bin/env bash
#-----------------------------------------------------------------------------
# scripts/preflight.sh — is this bench fit to run a hardware test right now?
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
#   ./scripts/preflight.sh --offline    tools + python env only (no boards)
#   ./scripts/preflight.sh --single     + board A reachable, lease free
#   ./scripts/preflight.sh --pair       + BOTH boards reachable, leases free
#
# Every hardware make target depends on this. The point is a FAST, LOUD failure:
# `make test-pair` with the bench powered down must come back in seconds saying
# which board is dark — not hang inside pytest waiting on an ssh that will never
# connect, and certainly not start poking a half-present pair.
#
# Nothing here touches /dev/mem, the PL, or the peer aperture. The heaviest
# thing it does on a board is `ssh <host> true`. Preflight itself can never
# wedge anything.
#
# Exit codes:  0 ready   1 a hard check failed   2 bad usage
#-----------------------------------------------------------------------------
set -euo pipefail
# shellcheck source=_common.sh
. "$(dirname "$(readlink -f "$0")")/_common.sh"

MODE=pair
case "${1:---pair}" in
    --offline) MODE=offline ;;
    --single)  MODE=single ;;
    --pair)    MODE=pair ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *)         err "unknown option '$1'"; exit 2 ;;
esac

FAILED=0
declare -a ROWS

row()      { ROWS+=("$1|$2|$3"); }
check_ok()   { row "$1" "OK"   "$2"; }
check_warn() { row "$1" "WARN" "$2"; }
check_bad()  { row "$1" "FAIL" "$2"; FAILED=1; }

#-----------------------------------------------------------------------------
# 1. Host tooling
#-----------------------------------------------------------------------------
check_tools() {
    # Hard requirements — without these nothing runs.
    local t
    for t in git ssh; do
        if have "${t}"; then check_ok "tool:${t}" "$(command -v "${t}")"
        else check_bad "tool:${t}" "not on PATH"; fi
    done

    local py; py="$(hetsoc_python)"
    if [ -x "${py}" ] || have "${py}"; then
        check_ok "python" "${py} ($("${py}" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || echo '?'))"
    else
        check_bad "python" "no usable interpreter (set HETSOC_PYTHON)"
    fi

    if [ -x "${HETSOC_VENV}/bin/python" ]; then
        check_ok "venv" "${HETSOC_VENV}"
    else
        check_bad "venv" "missing — run 'make deps' (or 'make venv')"
    fi

    if "${py}" -c 'import hetsoc' 2>/dev/null; then
        check_ok "hetsoc" "importable"
    else
        check_bad "hetsoc" "\`import hetsoc\` fails — run 'make deps'; host/hetsoc/ may not have landed yet"
    fi

    # Soft requirements — degrade, do not stop.
    for t in fpgahub sshpass; do
        if have "${t}"; then check_ok "tool:${t}" "$(command -v "${t}")"
        else check_warn "tool:${t}" "absent (see notes below)"; fi
    done
}

#-----------------------------------------------------------------------------
# 2. fpgahub — daemon reachable, and the board's lease is free
#-----------------------------------------------------------------------------
# One `status --json` call, parsed once. jq is NOT installed on this host, so
# the parser is python. Emits `key=value` lines per requested board.
hub_snapshot() {
    local py; py="$(hetsoc_python)"
    fpgahub status --json 2>/dev/null | "${py}" -c '
import json, sys
want = sys.argv[1:]
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(1)
by = {b.get("name"): b for b in doc.get("boards", [])}
for name in want:
    b = by.get(name)
    if b is None:
        print("%s|absent||||" % name)
        continue
    print("%s|present|%s|%s|%s|%s" % (
        name,
        "yes" if b.get("in_use") else "no",
        b.get("lease_state") or "none",
        b.get("lease_holder") or "",
        b.get("host_ssh") or "",
    ))
' "$@"
}

check_fpgahub() {
    local boards=("$@")

    if ! have fpgahub; then
        check_warn "fpgahub" "not installed — leasing and JTAG POR recovery unavailable"
        return 0
    fi
    if ! fpgahub health >/dev/null 2>&1; then
        check_warn "fpgahub" "daemon not reachable — leasing and POR recovery unavailable"
        return 0
    fi
    check_ok "fpgahub" "daemon reachable"

    local snap
    if ! snap="$(hub_snapshot "${boards[@]}")" || [ -z "${snap}" ]; then
        check_warn "fpgahub:status" "could not read 'fpgahub status --json'"
        return 0
    fi

    local name present in_use state holder host_ssh
    while IFS='|' read -r name present in_use state holder host_ssh; do
        [ -n "${name}" ] || continue
        if [ "${present}" != "present" ]; then
            check_bad "hub:${name}" "board is not in the fpgahub inventory"
            continue
        fi
        if [ "${in_use}" = "yes" ]; then
            # Ours is fine; anyone else's blocks. A lease we forgot to release
            # is the single most common reason a bench run mysteriously stalls.
            local mine=""
            if [ -f "${HETSOC_BUILD}/lease.env" ]; then
                mine="$(sed -n 's/^HETSOC_LEASE_HOLDER=//p' "${HETSOC_BUILD}/lease.env" | tr -d '"')"
            fi
            if [ -n "${mine}" ] && [ "${holder}" = "${mine}" ]; then
                check_ok "hub:${name}" "leased by us (${holder})"
            else
                check_bad "hub:${name}" "LEASED by '${holder}' (state=${state}) — 'make release' if that lease is yours and stale"
            fi
        else
            check_ok "hub:${name}" "free (${host_ssh:-no host_ssh})"
        fi
    done <<<"${snap}"
}

#-----------------------------------------------------------------------------
# 3. Boards — reachable over ssh
#-----------------------------------------------------------------------------
check_board() {
    local board="$1" host verdict
    if ! host="$(board_host "${board}")" || [ -z "${host}" ]; then
        check_bad "ssh:${board}" "no ssh endpoint (not in fpgahub, no set_env.sh default)"
        return
    fi
    verdict="$(board_probe "${host}")"
    case "${verdict}" in
        ok)
            check_ok "ssh:${board}" "${host}" ;;
        unreachable)
            check_bad "ssh:${board}" "${host}: $(probe_detail "${verdict}") Recover with 'make bench-recover BOARD=${board}'. See docs/SAFETY.md." ;;
        *)
            # Alive but we cannot drive it. Still a hard FAIL — the tests need
            # ssh — but emphatically NOT a POR candidate.
            check_bad "ssh:${board}" "${host}: $(probe_detail "${verdict}")" ;;
    esac
}

#-----------------------------------------------------------------------------
# run
#-----------------------------------------------------------------------------
declare -a BOARDS=()
case "${MODE}" in
    offline) ;;
    single)  BOARDS=("${HETSOC_BOARD_A}") ;;
    pair)    BOARDS=("${HETSOC_BOARD_A}" "${HETSOC_BOARD_B}") ;;
esac

# Expand the array only when it is non-empty: referencing an empty array under
# `set -u` is an error on bash <= 4.4, and this host is 4.4.20.
BOARD_LIST=""
if [ "${#BOARDS[@]}" -gt 0 ]; then
    BOARD_LIST=" — boards: ${BOARDS[*]}"
fi
log "preflight (${MODE})${BOARD_LIST}"

check_tools
if [ "${#BOARDS[@]}" -gt 0 ]; then
    check_fpgahub "${BOARDS[@]}"
    for b in "${BOARDS[@]}"; do
        check_board "${b}"
    done
fi

echo
hr
printf '  %-18s %-6s %s\n' "CHECK" "STATE" "DETAIL"
hr
for r in "${ROWS[@]}"; do
    IFS='|' read -r n v d <<<"${r}"
    case "${v}" in
        OK)   printf "  %-18s ${C_GRN}%-6s${C_OFF} %s\n" "${n}" "${v}" "${d}" ;;
        WARN) printf "  %-18s ${C_YEL}%-6s${C_OFF} %s\n" "${n}" "${v}" "${d}" ;;
        *)    printf "  %-18s ${C_RED}%-6s${C_OFF} %s\n" "${n}" "${v}" "${d}" ;;
    esac
done
hr

if [ "${FAILED}" -ne 0 ]; then
    err "preflight FAILED — refusing to touch the bench."
    err "Nothing was deployed, written or probed. Fix the FAIL rows above."
    exit 1
fi
ok "preflight passed (${MODE})"
