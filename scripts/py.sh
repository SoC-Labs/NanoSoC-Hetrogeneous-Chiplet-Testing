#!/usr/bin/env bash
#-----------------------------------------------------------------------------
# scripts/py.sh — run a python script with the framework's interpreter.
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
# The venv's python if there is one, otherwise the newest system python
# set_env.sh could find. Exists so no Makefile recipe ever hard-codes
# `python3` — the default python3 on this host is 3.8, too old for tomllib.
#
#   ./scripts/py.sh ci/results_to_junit.py build/results -o build/results/junit.xml
#-----------------------------------------------------------------------------
set -euo pipefail
# shellcheck source=_common.sh
. "$(dirname "$(readlink -f "$0")")/_common.sh"

[ "$#" -ge 1 ] || die "usage: py.sh <script.py> [args...]"
exec "$(hetsoc_python)" "$@"
