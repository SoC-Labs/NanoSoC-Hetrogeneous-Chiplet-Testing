#!/usr/bin/env bash
#-----------------------------------------------------------------------------
# scripts/lint.sh — the static gate: python lint + shell lint. No boards, no
#                   EDA licence, no network. Runs anywhere, always.
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
#   ./scripts/lint.sh          check only  (make lint)
#   ./scripts/lint.sh --fix    apply ruff format + ruff --fix  (make fmt)
#
# Every tool is optional and degrades to a WARN row, because a fresh clone on a
# machine without ruff should still be able to run the shell lint — and a lint
# script that refuses to start is a lint script nobody runs. It only exits
# non-zero on an actual finding.
#
# deps/ is excluded throughout: those are other repos with their own gates.
#-----------------------------------------------------------------------------
set -uo pipefail
# shellcheck source=_common.sh
. "$(dirname "$(readlink -f "$0")")/_common.sh"

FIX=0
case "${1:-}" in
    "")        ;;
    --fix)     FIX=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *)         err "unknown option '$1' (--fix)"; exit 2 ;;
esac

FAILED=0
declare -a ROWS
pass_row() { ROWS+=("$1|PASS|$2"); }
fail_row() { ROWS+=("$1|FAIL|$2"); FAILED=1; }
skip_row() { ROWS+=("$1|SKIP|$2"); }

cd "${HETSOC_ROOT}" || die "cannot cd to ${HETSOC_ROOT}"

# Directories that are never ours to lint. `build` matters as much as `deps`:
# VCS drops generated shell (csrc/clean.sh, full of legacy backticks) under
# sim/*/build/, and linting another tool's output is pure noise.
PRUNE=(-path ./deps -o -path ./.venv -o -path ./.git
       -o -name build -o -name sim_build -o -name csrc
       -o -name '*.egg-info' -o -name __pycache__)

PY="$(hetsoc_python)"

# THE GATING RULE SET IS PINNED HERE, ON PURPose, rather than left to ruff's
# defaults. Two reasons:
#
#  1. Ruff's default selection changes between releases. A gate that silently
#     widens on `pip install -U ruff` turns a green repo red for reasons nobody
#     asked for, and the first fix people reach for is to stop running it.
#  2. E (pycodestyle errors) + F (pyflakes) is the "this is a defect" tier:
#     undefined names, unused imports, syntax-adjacent mistakes. The
#     modernisation rules (UP*) would flag ~400 uses of %-formatting across
#     host/ and tests/ — which is the house style throughout this lab
#     (kr260_eth_regress.py is written the same way). Failing the build over
#     that is noise, not signal.
#
# The wider set still runs, as a non-gating advisory row.
#
# Line length 100: the default 88 is too tight for the address-map constants
# and long log strings this codebase is full of.
RUFF_GATE=(--select "E,F" --line-length 100)

# Python source we own. host/ and tests/ belong to other areas but are linted
# here so one command covers the repo.
declare -a PY_TARGETS=()
for d in host tests ci scripts sim flows; do
    [ -d "${d}" ] || continue
    # -quit on the first hit: cheap, and avoids depending on globstar.
    if [ -n "$(find "${d}" \( "${PRUNE[@]}" \) -prune -o -name '*.py' -type f -print -quit)" ]; then
        PY_TARGETS+=("${d}")
    fi
done

#-----------------------------------------------------------------------------
# 1. python — ruff preferred, flake8 accepted, compileall as the last resort
#-----------------------------------------------------------------------------
lint_python() {
    if [ "${#PY_TARGETS[@]}" -eq 0 ]; then
        skip_row "python" "no .py files yet outside deps/"
        return
    fi

    local ruff=""
    if [ -x "${HETSOC_VENV}/bin/ruff" ]; then ruff="${HETSOC_VENV}/bin/ruff"
    elif have ruff; then ruff="$(command -v ruff)"; fi

    if [ -n "${ruff}" ]; then
        if [ "${FIX}" -eq 1 ]; then
            log "ruff format + --fix over: ${PY_TARGETS[*]}"
            "${ruff}" format "${RUFF_GATE[@]}" "${PY_TARGETS[@]}" || true
            "${ruff}" check --fix "${RUFF_GATE[@]}" "${PY_TARGETS[@]}" || true
        fi

        log "ruff check (gating: ${RUFF_GATE[*]}) over: ${PY_TARGETS[*]}"
        if "${ruff}" check "${RUFF_GATE[@]}" "${PY_TARGETS[@]}"; then
            pass_row "ruff" "E,F clean over ${PY_TARGETS[*]}"
        else
            fail_row "ruff" "findings above"
        fi

        # Advisory pass: everything ruff's current defaults have an opinion
        # about. Reported as a count, never gating — see RUFF_GATE above.
        local advisory
        advisory="$("${ruff}" check --statistics "${PY_TARGETS[@]}" 2>/dev/null | tail -n +1 | wc -l)"
        if [ "${advisory}" -gt 0 ]; then
            skip_row "ruff-style" "${advisory} advisory rule(s) beyond the gate — 'ruff check ${PY_TARGETS[*]}' to see them"
        fi
        return
    fi

    if have flake8; then
        log "flake8 over: ${PY_TARGETS[*]} (ruff not installed)"
        if flake8 --max-line-length=100 "${PY_TARGETS[@]}"; then
            pass_row "flake8" "${PY_TARGETS[*]}"
        else
            fail_row "flake8" "findings above"
        fi
        return
    fi

    # Neither linter present. A syntax check is still worth something: it
    # catches the class of breakage that stops the suite collecting at all.
    warn "neither ruff nor flake8 found — falling back to a syntax check only"
    if "${PY}" -m compileall -q "${PY_TARGETS[@]}" >/dev/null; then
        pass_row "py-syntax" "compileall clean (install ruff for a real lint)"
    else
        fail_row "py-syntax" "syntax errors"
    fi
}

#-----------------------------------------------------------------------------
# 2. shell — shellcheck over everything we own
#-----------------------------------------------------------------------------
lint_shell() {
    local -a sh_files=()
    local f
    while IFS= read -r f; do
        sh_files+=("${f}")
    done < <(find . \( "${PRUNE[@]}" \) -prune -o -name '*.sh' -type f -print | sort)

    if [ "${#sh_files[@]}" -eq 0 ]; then
        skip_row "shellcheck" "no .sh files"
        return
    fi
    if ! have shellcheck; then
        skip_row "shellcheck" "not installed (${#sh_files[@]} scripts unchecked)"
        # Still prove they parse — a syntax error here breaks every make target.
        local bad=0
        for f in "${sh_files[@]}"; do
            bash -n "${f}" || bad=1
        done
        if [ "${bad}" -eq 0 ]; then
            pass_row "bash -n" "${#sh_files[@]} scripts parse"
        else
            fail_row "bash -n" "syntax error (see above)"
        fi
        return
    fi

    log "shellcheck over ${#sh_files[@]} scripts"
    # -x follows `source`d files so _common.sh's helpers are known; -P SCRIPTDIR
    # tells it where to look, which it cannot work out for itself when the path
    # is built by a command substitution (every script here does
    # `. "$(dirname "$(readlink -f "$0")")/_common.sh"`).
    if shellcheck -x -P SCRIPTDIR -S style "${sh_files[@]}"; then
        pass_row "shellcheck" "${#sh_files[@]} scripts clean (severity=style)"
    else
        fail_row "shellcheck" "findings above"
    fi
}

#-----------------------------------------------------------------------------
# 3. the Makefile actually parses
#-----------------------------------------------------------------------------
lint_make() {
    if make -n help >/dev/null 2>&1; then
        pass_row "makefile" "'make help' parses"
    else
        fail_row "makefile" "'make -n help' failed"
    fi
}

hr
log "static gate: python + shell + make"
hr
lint_python
lint_shell
lint_make

echo
hr
printf '  %-14s %-6s %s\n' "GATE" "RESULT" "DETAIL"
hr
for r in "${ROWS[@]}"; do
    IFS='|' read -r n v d <<<"${r}"
    printf '  %-14s %-6s %s\n' "${n}" "${v}" "${d}"
done
hr
if [ "${FAILED}" -eq 0 ]; then
    ok "lint clean"
else
    err "lint FAILED"
fi
exit "${FAILED}"
