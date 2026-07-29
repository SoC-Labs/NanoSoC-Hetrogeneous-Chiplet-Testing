#!/usr/bin/env bash
#-----------------------------------------------------------------------------
# flows/lease.sh — take / give back the fpgahub lease on the two-board pair.
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
#   ./flows/lease.sh acquire     take both boards, record build/lease.env
#   ./flows/lease.sh release     hand them back
#   ./flows/lease.sh status      who holds what
#
# WHY LEASE AT ALL: these two KR260s are shared. A second person deploying a
# bitstream underneath a running pair test does not produce a test failure — it
# produces a wedge, and then two people are confused instead of one.
#
# WHY THE TOKEN GOES IN A FILE: `fpgahub lease release` REQUIRES --token, and
# the token is only ever printed once, by acquire. Lose it and the lease sits
# there until its TTL expires, blocking the bench and making everyone else's
# preflight fail with "LEASED by ...". build/lease.env is that memory.
#
# `lease acquire`/`release` are COLLECTION endpoints, so unlike `target reset`
# they work fine from this dev host — no mapstone-dev hop needed. See
# flows/recover.sh for the endpoints that do need one.
#
# ATOMICITY: two independent per-board leases are not a pair lease. If the
# second acquire fails we release the first rather than half-hold the bench.
#-----------------------------------------------------------------------------
set -uo pipefail
# shellcheck source=../scripts/_common.sh
. "$(dirname "$(readlink -f "$0")")/../scripts/_common.sh"

ACTION="${1:-status}"
TTL="${HETSOC_LEASE_TTL:-3600}"
HOLDER="${HETSOC_LEASE_HOLDER:-hetsoc-$(hostname -s)-$$}"
LEASE_ENV="${HETSOC_BUILD}/lease.env"
BOARDS=("${HETSOC_BOARD_A}" "${HETSOC_BOARD_B}")

have fpgahub || die "fpgahub is not installed — cannot lease. Coordinate on the bench by hand."

# `lease acquire` has no --json, so the token is scraped from its output.
# Prefer a run of token-ish characters on a line that actually says "token";
# fall back to the longest such run anywhere. The full output is echoed either
# way, so a future format change is visible rather than silently mis-parsed.
extract_token() {
    local out="$1" tok
    tok="$(printf '%s\n' "${out}" \
           | sed -n 's/.*[Tt]oken[^A-Za-z0-9_-]\{1,\}\([A-Za-z0-9_-]\{16,\}\).*/\1/p' \
           | tail -1)"
    if [ -z "${tok}" ]; then
        tok="$(printf '%s\n' "${out}" | grep -oE '[A-Za-z0-9_-]{24,}' | tail -1)"
    fi
    printf '%s' "${tok}"
}

# The two KR260s sit in a group alongside their _pl (LAN8720) members, and the
# API rejects a single-board lease on a grouped board with 409 'pair_required'.
# --solo asks for the PS-side board on its own — but it is a newer-CLI flag and
# the daemon here is older, so try it and fall back rather than assume.
acquire_one() {
    local b="$1"
    local out rc
    out="$(fpgahub lease acquire "${b}" --holder "${HOLDER}" --ttl "${TTL}" --solo 2>&1)"; rc=$?
    if [ "${rc}" -ne 0 ] && printf '%s' "${out}" | grep -qiE 'no such option|unexpected extra|unrecognized'; then
        out="$(fpgahub lease acquire "${b}" --holder "${HOLDER}" --ttl "${TTL}" 2>&1)"; rc=$?
    fi
    printf '%s' "${out}"
    return "${rc}"
}

do_acquire() {
    mkdir -p "${HETSOC_BUILD}"
    : >"${LEASE_ENV}.tmp"
    local -a got=()
    local b out token rc

    for b in "${BOARDS[@]}"; do
        log "acquiring ${b} (holder=${HOLDER} ttl=${TTL}s)"
        out="$(acquire_one "${b}")"; rc=$?
        printf '%s\n' "${out}" | sed 's/^/    /'
        if [ "${rc}" -ne 0 ]; then
            err "could not acquire ${b}"
            if [ "${#got[@]}" -gt 0 ]; then
                warn "rolling back the ${#got[@]} lease(s) already taken"
                mv "${LEASE_ENV}.tmp" "${LEASE_ENV}"
                printf 'HETSOC_LEASE_HOLDER="%s"\nHETSOC_LEASE_BOARDS="%s"\n' \
                    "${HOLDER}" "${got[*]}" >>"${LEASE_ENV}"
                do_release_recorded
            fi
            rm -f "${LEASE_ENV}.tmp"
            return 1
        fi
        token="$(extract_token "${out}")"
        if [ -z "${token}" ]; then
            warn "${b}: acquired but no token parsed from the output —"
            warn "         'make release' will not work; release by hand."
        fi
        got+=("${b}")
        printf 'HETSOC_LEASE_TOKEN_%s="%s"\n' "${b}" "${token}" >>"${LEASE_ENV}.tmp"
    done

    {
        printf '# written by flows/lease.sh at %s\n' "$(date -Is)"
        printf 'HETSOC_LEASE_HOLDER="%s"\n' "${HOLDER}"
        printf 'HETSOC_LEASE_BOARDS="%s"\n' "${BOARDS[*]}"
        cat "${LEASE_ENV}.tmp"
    } >"${LEASE_ENV}"
    rm -f "${LEASE_ENV}.tmp"

    ok "leased: ${got[*]}  (holder=${HOLDER})"
    log "token(s) recorded in ${LEASE_ENV} — 'make release' when you are done."
    log "The lease expires by itself after ${TTL}s; heartbeat is not implemented"
    log "here because every flow in this repo is far shorter than that."
}

do_release_recorded() {
    if [ ! -f "${LEASE_ENV}" ]; then
        warn "no ${LEASE_ENV} — nothing recorded to release."
        warn "If a lease of yours is stuck: fpgahub lease acquire <board> prints a"
        warn "fresh token you can immediately release with."
        return 0
    fi
    # shellcheck source=/dev/null
    . "${LEASE_ENV}"
    local b tokvar token rc=0
    # Deliberately unquoted: HETSOC_LEASE_BOARDS is a space-separated list.
    # shellcheck disable=SC2086
    for b in ${HETSOC_LEASE_BOARDS:-${BOARDS[*]}}; do
        tokvar="HETSOC_LEASE_TOKEN_${b}"
        token="${!tokvar:-}"
        if [ -z "${token}" ]; then
            warn "${b}: no token recorded — skipping (release by hand)"
            continue
        fi
        log "releasing ${b}"
        if fpgahub lease release "${b}" --holder "${HETSOC_LEASE_HOLDER}" --token "${token}"; then
            ok "${b} released"
        else
            err "${b} release failed"
            rc=1
        fi
    done
    rm -f "${LEASE_ENV}"
    return "${rc}"
}

do_status() {
    local b
    for b in "${BOARDS[@]}"; do
        printf '  %-12s in_use=%-4s state=%-10s holder=%s\n' \
            "${b}" \
            "$(fpgahub_field "${b}" in_use 2>/dev/null || echo '?')" \
            "$(fpgahub_field "${b}" lease_state 2>/dev/null || echo '?')" \
            "$(fpgahub_field "${b}" lease_holder 2>/dev/null || echo '-')"
    done
    if [ -f "${LEASE_ENV}" ]; then
        log "we hold a lease per ${LEASE_ENV}"
    fi
}

case "${ACTION}" in
    acquire) do_acquire ;;
    release) do_release_recorded ;;
    status)  do_status ;;
    *)       err "usage: lease.sh acquire|release|status"; exit 2 ;;
esac
