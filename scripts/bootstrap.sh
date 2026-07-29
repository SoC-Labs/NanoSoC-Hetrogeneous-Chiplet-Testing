#!/usr/bin/env bash
#-----------------------------------------------------------------------------
# scripts/bootstrap.sh — one-shot setup for a fresh checkout.
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
#   ./scripts/bootstrap.sh                # submodules (shallow) + venv   [make deps]
#   ./scripts/bootstrap.sh --full         # + every nested submodule       [make deps-full]
#   ./scripts/bootstrap.sh --venv-only    # skip git entirely              [make venv]
#
# WHY THIS IS NOT `git submodule update --init --recursive`
# --------------------------------------------------------
# Recursing blindly from here pulls ~45 submodules eight levels deep across both
# chiplets — the whole ASIC flow, the Arm IP wrappers, two nanoSoC SoCs — which
# is minutes of clone for a repo whose primary deliverable is a *pytest suite
# that needs none of it*. Worse, one submodule inside TideLink
# (`deps/tidelink-phy`) is declared over SSH at the pinned commit, so a plain
# recursive init fails outright on a machine without SoTON SSH keys.
#
# So the default takes exactly two bites:
#
#   1. the two chiplet designs (deps/eth-chiplet, deps/compute-chiplet)
#   2. inside eth-chiplet only, `tidelink` + `tidechart` — the D2D IP the bench
#      tooling shells out to (kr260_eth_run.sh, deploy_pair_role)
#
# and `--full` delegates to each chiplet's OWN scripts/bootstrap.sh, which
# already carries the SSH→HTTPS rewrite for the nested clone. We do not
# reimplement that rewrite here; the chiplet repo owns its own fetch policy.
#
# WHY TIDELINK IS NOT A DIRECT SUBMODULE OF THIS REPO
# ---------------------------------------------------
# It is already a submodule of both chiplets, at DIFFERENT commits. Adding a
# third pin here would create a checkout that matches neither die — the lab's
# "two-checkouts trap", where the tree you read is not the tree that built the
# bitstream on the board. We reach it at deps/eth-chiplet/tidelink and let the
# eth-chiplet pin be authoritative. See docs/CI.md § Submodule versioning.
#-----------------------------------------------------------------------------
set -euo pipefail
# shellcheck source=_common.sh
. "$(dirname "$(readlink -f "$0")")/_common.sh"

MODE=default
case "${1:-}" in
    "")            MODE=default ;;
    --full)        MODE=full ;;
    --venv-only)   MODE=venv ;;
    -h|--help)     sed -n '2,45p' "$0"; exit 0 ;;
    *)             die "unknown option '$1' (--full | --venv-only)" ;;
esac

cd "${HETSOC_ROOT}"

# --- 1. submodules -----------------------------------------------------------
fetch_submodules() {
    if [ ! -f "${HETSOC_ROOT}/.gitmodules" ]; then
        warn "no .gitmodules — nothing to fetch"
        return 0
    fi

    log "fetching chiplet submodules (deps/eth-chiplet, deps/compute-chiplet)"
    git submodule update --init deps/eth-chiplet deps/compute-chiplet

    # TideLink + TideChart, reached through each chiplet's own pin. NOT
    # --recursive: we want exactly these, and nothing beneath them.
    #
    # BOTH chiplets, not just eth: each die's FPGA flow (`deploy_pair_role`,
    # kr260_deploy.sh) lives inside THAT die's TideLink checkout, so
    # flows/deploy_pair.sh needs both to say anything truthful about the pair.
    # `submodule sync` first — the eth-chiplet branch we pin declares TideLink
    # over a different remote than main does, and a stale URL in .git/config
    # silently fetches the wrong line.
    local repo name
    for repo in "${HETSOC_ETH_CHIPLET}" "${HETSOC_COMPUTE_CHIPLET}"; do
        [ -f "${repo}/.gitmodules" ] || continue
        name="$(basename "${repo}")"
        log "fetching D2D IP inside ${name} (tidelink, tidechart)"
        git -C "${repo}" submodule sync --quiet tidelink tidechart 2>/dev/null || true
        git -C "${repo}" \
            -c 'url.https://git.soton.ac.uk/.insteadOf=git@git.soton.ac.uk:' \
            submodule update --init tidelink tidechart
    done

    # A failed clone leaves an EMPTY directory behind and `submodule update` can
    # still exit 0 having skipped it. `submodule status` prefixes '-' to every
    # path that was never populated — that is the signal to check, not $?.
    local missing
    missing="$(git submodule status | sed -n 's/^-[0-9a-f]* //p' || true)"
    if [ -n "${missing}" ]; then
        err "these submodules were never populated:"
        printf '%s\n' "${missing}" | sed 's/^/    /' >&2
        die "bootstrap incomplete"
    fi

    log "submodule pins:"
    git submodule status | sed 's/^/    /'
    if [ -d "${TIDELINK_HOME}/.git" ] || [ -f "${TIDELINK_HOME}/.git" ]; then
        printf '    (D2D IP) tidelink  %s\n' "$(git -C "${TIDELINK_HOME}" rev-parse --short HEAD)"
    fi

    # The heterogeneous hazard, surfaced at fetch time rather than at 2am on the
    # bench: the two dies may be built against different TideLink commits, and a
    # protocol-level skew between them looks exactly like a bad ribbon.
    local tl_a tl_b
    tl_a="$(git -C "${HETSOC_ETH_CHIPLET}" submodule status tidelink 2>/dev/null | awk '{print $1}' | tr -d '+-' || true)"
    tl_b="$(git -C "${HETSOC_COMPUTE_CHIPLET}" submodule status tidelink 2>/dev/null | awk '{print $1}' | tr -d '+-' || true)"
    if [ -n "${tl_a}" ] && [ -n "${tl_b}" ] && [ "${tl_a}" != "${tl_b}" ]; then
        warn "the two dies pin DIFFERENT TideLink commits:"
        warn "    eth-chiplet     ${tl_a}"
        warn "    compute-chiplet ${tl_b}"
        warn "That is legal (they are different designs) but it is the first"
        warn "thing to check if the het pair fails to reach FCSM=4."
        warn "See docs/CI.md § Submodule versioning."
    fi
}

# --- known-broken upstream pin: tidelink-gpio-phy ---------------------------
# eth-chiplet's TideLink declares deps/tidelink-gpio-phy over
#   git@github.com:SoC-Labs/TideLink-Chiplet-GPIO-PHY.git
# and pins a commit that remote DOES NOT HAVE:
#   fatal: remote error: upload-pack: not our ref 6ee8418...
# The compute side declares the SAME submodule over the GitLab mirror and
# fetches the SAME commit without complaint — the GitHub repo is behind.
#
# This must be repaired or the sim cannot build, and it fails in a way nobody
# would connect to a submodule: the flists reference RTL inside it
# (WavResetSync.v), so VCS dies LATE with
#   Error-[CFCILFBI] Cannot find cell in liblist
# naming a *cell*, not a missing file.
#
# NOTE `git submodule sync` rewrites .git/config FROM .gitmodules, so a
# `git config submodule.<n>.url` override set before a sync is silently
# discarded. And submodule fetches over a local path are refused outright
# ("transport 'file' not allowed", CVE-2022-39253). So we bypass the submodule
# machinery: clone/fetch the work tree directly, then check the pin out.
GPIO_PHY_GITLAB="https://git.soton.ac.uk/soclabs/tidelink-gpio-phy.git"

repair_gpio_phy_pin() {
    local tl="$1" label="$2"
    local sub="deps/tidelink-gpio-phy"
    local path="${tl}/${sub}" sha src

    [ -d "${tl}" ] || return 0

    sha="$(git -C "${tl}" ls-tree HEAD "${sub}" 2>/dev/null | awk '{print $3}')"
    [ -n "${sha}" ] || return 0

    # Check the COMMIT, not merely that the directory is non-empty.
    #
    # The dangerous case is not an empty checkout — it is a POPULATED one at the
    # WRONG commit. When the pinned sha is unavailable, `git submodule update`
    # prints its fatal, leaves the clone parked on the remote's default branch
    # (5c76e76, a *different* PHY revision), and the outer bootstrap continues.
    # An emptiness test passes; the build then silently compiles the wrong PHY.
    local have
    have="$(git -C "${path}" rev-parse HEAD 2>/dev/null || true)"
    [ "${have}" = "${sha}" ] && return 0

    if [ -n "${have}" ]; then
        warn "${label}: ${sub} is at the WRONG commit (known-broken upstream pin)"
        warn "    want ${sha}"
        warn "    have ${have}  <-- would build different PHY RTL"
    else
        warn "${label}: ${sub} is empty (known-broken upstream pin) — repairing"
    fi

    # 1) the GitLab mirror, which demonstrably carries this commit
    # 2) a peer checkout that already has it (the other chiplet, usually)
    # 3) an operator-supplied path
    for src in "${GPIO_PHY_GITLAB}" \
               "${HETSOC_COMPUTE_CHIPLET}/tidelink/${sub}" \
               "${HETSOC_ETH_CHIPLET}/tidelink/${sub}" \
               "${HETSOC_GPIO_PHY_SRC:-}"; do
        [ -n "${src}" ] || continue
        case "${src}" in "${path}") continue ;; esac          # never self
        case "${src}" in /*) [ -d "${src}/.git" ] || [ -f "${src}/.git" ] || continue ;; esac

        if [ ! -e "${path}/.git" ]; then
            git clone --quiet "${src}" "${path}" 2>/dev/null || { rm -rf "${path}"; continue; }
        fi
        # --force: the pinned sha is frequently NOT on any branch of the source
        # (that is the whole defect), so a plain refspec fetch will not bring it.
        git -C "${path}" fetch --quiet --force "${src}" \
            "${sha}:refs/hetsoc/gpio-phy-pin" 2>/dev/null \
            || git -C "${path}" fetch --quiet "${src}" "${sha}" 2>/dev/null \
            || true
        if git -C "${path}" cat-file -e "${sha}^{commit}" 2>/dev/null; then
            git -C "${path}" checkout --quiet "${sha}"
            log "${label}: ${sub} -> ${sha} (via ${src})"
            return 0
        fi
    done

    err "${label}: could not obtain ${sub} @ ${sha} from any source."
    err "    Set HETSOC_GPIO_PHY_SRC to a checkout that has that commit."
    err "    Real fix is upstream — see docs/SIM_PLAN.md §10a."
    return 1
}

# Every nested TideLink dep the sim flists reference. Checks the COMMIT, not
# just presence: empty gives a late, confusing VCS error, but WRONG-COMMIT gives
# no error at all and silently builds different RTL.
verify_tidelink_deps() {
    local repo tl dep bad=0 want have
    for repo in "${HETSOC_ETH_CHIPLET}" "${HETSOC_COMPUTE_CHIPLET}"; do
        tl="${repo}/tidelink"
        [ -d "${tl}" ] || continue
        for dep in axi-chiplet-controller tidelink-phy tidelink-gpio-phy; do
            [ -d "${tl}/deps/${dep}" ] || continue
            want="$(git -C "${tl}" ls-tree HEAD "deps/${dep}" 2>/dev/null | awk '{print $3}')"
            have="$(git -C "${tl}/deps/${dep}" rev-parse HEAD 2>/dev/null || true)"
            if [ -z "${have}" ]; then
                err "empty nested submodule: $(basename "${repo}")/tidelink/deps/${dep}"
                bad=1
            elif [ -n "${want}" ] && [ "${want}" != "${have}" ]; then
                err "WRONG COMMIT: $(basename "${repo}")/tidelink/deps/${dep}"
                err "    want ${want}"
                err "    have ${have}"
                bad=1
            fi
        done
    done
    if [ "${bad}" = "1" ]; then
        err "The sim flists reference RTL inside these. An EMPTY dep makes VCS"
        err "fail late with 'Cannot find cell in liblist'; a WRONG-COMMIT dep"
        err "does not fail at all — it silently builds different RTL."
        die "bootstrap incomplete (nested TideLink deps)"
    fi
    log "nested TideLink deps present and at the pinned commits on both dies"
}

fetch_submodules_full() {
    fetch_submodules
    local repo
    for repo in "${HETSOC_ETH_CHIPLET}" "${HETSOC_COMPUTE_CHIPLET}"; do
        # NOTE: `|| warn`, deliberately. The eth chiplet's own bootstrap EXITS
        # NON-ZERO on the known-broken tidelink-gpio-phy pin (see
        # repair_gpio_phy_pin). Under `set -e` that would abort us here — before
        # the repair that fixes exactly this — so the failure is downgraded to a
        # warning and verify_tidelink_deps below is what actually gates.
        if [ -x "${repo}/scripts/bootstrap.sh" ]; then
            log "delegating full fetch to $(basename "${repo}")/scripts/bootstrap.sh"
            "${repo}/scripts/bootstrap.sh" \
                || warn "$(basename "${repo}")/scripts/bootstrap.sh exited non-zero — continuing to repair+verify"
        else
            log "recursive submodule fetch in $(basename "${repo}") (no bootstrap.sh)"
            git -C "${repo}" \
                -c 'url.https://git.soton.ac.uk/.insteadOf=git@git.soton.ac.uk:' \
                submodule update --init --recursive \
                || warn "recursive fetch in $(basename "${repo}") exited non-zero — continuing to repair+verify"
        fi
    done

    # Repair the eth side AFTER the compute side has run: the compute checkout
    # is one of the sources we can fall back to.
    repair_gpio_phy_pin "${HETSOC_COMPUTE_CHIPLET}/tidelink" "compute-chiplet" || true
    repair_gpio_phy_pin "${HETSOC_ETH_CHIPLET}/tidelink"     "eth-chiplet"
    verify_tidelink_deps
}

# --- 2. python -------------------------------------------------------------
build_venv() {
    local py="${HETSOC_PYTHON}"
    local ver
    ver="$("${py}" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
    log "python ${ver} (${py})"
    case "${ver}" in
        3.8|3.7|3.6|2.*) die "python ${ver} is too old — need 3.9+, 3.11+ preferred (tomllib). Set HETSOC_PYTHON." ;;
    esac
    if [ "${ver}" != "3.11" ] && [ "${ver}" != "3.12" ] && [ "${ver}" != "3.13" ]; then
        warn "python ${ver} has no stdlib tomllib; hetsoc.config may need the tomli backport."
    fi

    if [ ! -x "${HETSOC_VENV}/bin/python" ]; then
        log "creating venv at ${HETSOC_VENV}"
        "${py}" -m venv "${HETSOC_VENV}"
    else
        log "venv already present at ${HETSOC_VENV}"
    fi

    local vpy="${HETSOC_VENV}/bin/python"
    "${vpy}" -m pip install --quiet --upgrade pip setuptools wheel

    if [ -f "${HETSOC_ROOT}/host/pyproject.toml" ]; then
        log "pip install -e host/"
        "${vpy}" -m pip install --quiet -e "${HETSOC_ROOT}/host"
    else
        warn "host/pyproject.toml does not exist yet — the framework area has not landed."
        warn "Installing the test/lint tooling only; hetsoc will resolve via PYTHONPATH."
        "${vpy}" -m pip install --quiet pytest
    fi

    # Lint/format tooling. Best-effort: an offline machine should still get a
    # working venv, it just will not be able to run `make lint`.
    if ! "${vpy}" -m pip install --quiet pytest ruff; then
        warn "could not install pytest/ruff into the venv (offline?) — make lint may fall back to system tools"
    fi

    if "${vpy}" -c 'import hetsoc' 2>/dev/null; then
        ok "import hetsoc works"
    else
        warn "\`import hetsoc\` still fails — expected until host/hetsoc/ lands."
    fi
}

# --- run ---------------------------------------------------------------------
hr
log "bootstrap: mode=${MODE}  root=${HETSOC_ROOT}"
hr

case "${MODE}" in
    default) fetch_submodules ;;
    full)    fetch_submodules_full ;;
    venv)    : ;;
esac
build_venv

hr
ok "bootstrap complete"
cat <<EOF

Next:
  source set_env.sh
  make test-offline          # L0 — no boards needed
  make bench-status          # L1 — read-only probe of both boards (cannot wedge)
  make help                  # everything else

Read docs/SAFETY.md before you touch a board.
EOF
