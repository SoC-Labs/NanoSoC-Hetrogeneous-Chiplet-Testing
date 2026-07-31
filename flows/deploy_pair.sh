#!/usr/bin/env bash
#-----------------------------------------------------------------------------
# flows/deploy_pair.sh — reflash BOTH dies of the heterogeneous pair.
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
#   ./flows/deploy_pair.sh                 eth on board A, compute on board B
#   ./flows/deploy_pair.sh --die a         one side only
#   ./flows/deploy_pair.sh --dry-run       print the commands, run nothing
#
# WHAT THIS IS: a thin wrapper. The actual deploy machinery lives in TideLink
# (fpga/Makefile `deploy_pair_role` -> pynq_host/scripts/kr260_deploy.sh):
# header-strip the .bin, scp it plus the PS-side tooling to ~/td, `fpgautil -b
# <bin> -f Full`, verify fpga_manager == operating, re-poke the AFI port widths,
# print the canaries. Reimplementing any of that here would fork it. We only
# add what is specific to a HETEROGENEOUS pair:
#
#   * two DIFFERENT designs, one per board, so the TIDELINK_HOME/FPGA_DIR each
#     invocation uses must come from that die's own chiplet checkout;
#   * the roles are fixed to the physical J21 wiring — die_a is the straight
#     pinout, die_b the mirrored one. Swapping which board is which means
#     swapping bitstreams, not just a flag.
#
# THE ORDERING RULE (printed as a banner too, because it is the one that bites):
#   POWER-CYCLE BOTH -> deploy die_a -> deploy die_b -> bring up.
# Never PL-reload one side of a LIVE link: the other side keeps its link state,
# the two desync, and the next peer access hangs the PS bus. That is a wedge,
# and a wedge needs a JTAG POR from another host.
#
# SEQUENTIAL, NOT CONCURRENT: deploy writes the PL: two `fpgautil` loads racing
# across a live ribbon is precisely the situation above. Bring-up (the next
# step, flows/bringup_pair.sh) is the concurrent one.
#-----------------------------------------------------------------------------
set -uo pipefail
# shellcheck source=../scripts/_common.sh
. "$(dirname "$(readlink -f "$0")")/../scripts/_common.sh"

DIE=both
DRY=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --die)     DIE="${2:-both}"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
        *)         err "unknown option '$1'"; exit 2 ;;
    esac
done
case "${DIE}" in a|b|both) ;; *) err "--die must be a, b or both"; exit 2 ;; esac

# Which chiplet checkout owns each side of the pair.
ETH_FPGA_DIR="${HETSOC_ETH_CHIPLET}/tidelink/fpga"
COMPUTE_FPGA_DIR="${HETSOC_COMPUTE_CHIPLET}/tidelink/fpga"

# TARGET names are TideLink fpga/targets/ entries. The eth die's targets are
# proven (kr260-eth-chiplet / -flip). The compute die's targets ARE NOW BUILT
# (2026-07-31, write_bitstream Complete, 0 critical warnings, WNS +12.30ns).
#
# NAMING GOTCHA — the compute chiplet's "flip" is INVERTED vs the eth chiplet's:
#   eth:      kr260-eth-chiplet      = die_a (straight balls),  -flip = die_b (mirrored)
#   compute:  kr260-compute-chiplet  = die_b (MIRRORED balls),  -flip = die_a (straight)
# i.e. compute's `-flip` is die_a (its balls match eth die_a), and the NON-flip
# kr260-compute-chiplet is die_b. So for the eth(die_a) -> compute(die_b) pair the
# compute side is the NON-flip target. (Verified: compute die_b fwd-clock TX=AC14
# RX=AD15 is the exact complement of eth die_a, so a straight J21 ribbon crosses
# TX<->RX correctly.)
ETH_TARGET_A="${HETSOC_ETH_TARGET_A:-kr260-eth-chiplet}"
COMPUTE_TARGET_B="${HETSOC_COMPUTE_TARGET_B:-kr260-compute-chiplet}"

banner() {
    printf '%s' "${C_YEL}"
    cat <<'EOF'
######################################################################
# PAIR DEPLOY                                                        #
#   1. POWER-CYCLE BOTH BOARDS FIRST                                 #
#   2. deploy die_a, then die_b (sequential, never concurrent)       #
#   3. THEN bring the link up on both, concurrently                  #
#                                                                    #
# Never PL-reload one side of a LIVE link — the two sides desync and #
# the next cross-die access wedges the PS bus.                       #
# Never 'sudo reboot' a KR260. JTAG POR only: make bench-recover.    #
######################################################################
EOF
    printf '%s' "${C_OFF}"
}

# deploy_one <role> <fpga_dir> <target> <board>
deploy_one() {
    local role="$1" fpga_dir="$2" target="$3" board="$4"
    local host
    host="$(board_host "${board}")" || { err "${board}: no ssh endpoint"; return 1; }

    if [ ! -f "${fpga_dir}/Makefile" ]; then
        err "${role}: no ${fpga_dir}/Makefile"
        err "       the chiplet submodule (or its tidelink) is not populated — run 'make deps'"
        return 1
    fi
    if [ ! -d "${fpga_dir}/targets/${target}" ]; then
        err "${role}: TideLink target '${target}' does not exist in ${fpga_dir}/targets/"
        err "       Available: $(find "${fpga_dir}/targets" -mindepth 1 -maxdepth 1 -type d -printf '%f ' 2>/dev/null)"
        err "       The compute die's targets ARE built: kr260-compute-chiplet (die_b),"
        err "       kr260-compute-chiplet-flip (die_a). For eth(die_a)->compute(die_b),"
        err "       die_b is the NON-flip: HETSOC_COMPUTE_TARGET_B=kr260-compute-chiplet."
        return 1
    fi

    local -a cmd=(make -C "${fpga_dir}" deploy_pair_role
                  SOC=kr260_eth "ROLE=${role}" "TARGET=${target}"
                  "KR260_HOST=${host}")
    [ -n "${HETSOC_PASSWORD:-}" ] && cmd+=("KR260_PASSWORD=${HETSOC_PASSWORD}")

    hr
    log "${role}: ${target} -> ${board} (${host})"
    if [ "${DRY}" -eq 1 ]; then
        printf '    %q ' "${cmd[@]}"; echo
        return 0
    fi
    "${cmd[@]}"
}

banner
if [ "${DRY}" -eq 0 ]; then
    log "5s to Ctrl-C if the boards have NOT been power-cycled..."
    sleep 5
fi

RC=0
if [ "${DIE}" = "a" ] || [ "${DIE}" = "both" ]; then
    deploy_one "${HETSOC_ROLE_A}" "${ETH_FPGA_DIR}" "${ETH_TARGET_A}" "${HETSOC_BOARD_A}" || RC=1
fi
if [ "${RC}" -eq 0 ] && { [ "${DIE}" = "b" ] || [ "${DIE}" = "both" ]; }; then
    deploy_one "${HETSOC_ROLE_B}" "${COMPUTE_FPGA_DIR}" "${COMPUTE_TARGET_B}" "${HETSOC_BOARD_B}" || RC=1
fi

hr
if [ "${RC}" -eq 0 ]; then
    ok "pair deploy complete — both dies are FRESH."
    log "Bring the link up now, on both dies together:  make bench-bringup"
    log "Fresh dies are the ONLY safe time to run bring-up; on a live link it"
    log "resets the link layer under a running peer and wedges the sender."
else
    err "pair deploy FAILED — do NOT bring up. Fix the above first."
fi
exit "${RC}"
