#!/usr/bin/env python3
# =============================================================================
# test_id_map.py — reconcile the two test-id namespaces and emit docs/TEST_ID_MAP.md
#
# WHY THIS EXISTS
# ---------------
# docs/TEST_MATRIX.md (the PLAN) and tests/ (the IMPLEMENTATION) were authored
# concurrently and chose different area taxonomies:
#
#   matrix : one area per functional TOPIC   (L2-CAM, L2-ROLE, L2-TC, L2-LINK)
#   pytest : one area per FILE               (L2-CFG-01..09)
#
# Both are defensible, and renumbering either by hand across ~143 + ~67 ids
# would churn every cross-reference for no behavioural gain. The actual hazard
# is narrower and nastier: 29 ids exist in BOTH namespaces, and several mean
# DIFFERENT THINGS in each --
#
#   L1-PROBE-03   matrix: "board reachable (ssh + sudo + /dev/mem)"
#                 pytest: "effective role matches the role deployed"
#   L3-LINK-05    matrix: "role asymmetry on silicon"
#                 pytest: "both dies have seen the CR and CRACK packets"
#
# An id that LOOKS like a match but is not is worse than no match: it silently
# licenses a wrong conclusion ("the matrix says this is PROVEN-HOM"). So rather
# than renumber, this script makes the mapping explicit, machine-generated and
# regenerable, and FAILS LOUD on a divergent collision.
#
#   ./scripts/test_id_map.py            # regenerate docs/TEST_ID_MAP.md
#   ./scripts/test_id_map.py --check    # exit 1 if the file is stale (CI)
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
import argparse
import difflib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MATRIX = ROOT / "docs" / "TEST_MATRIX.md"
TESTS = ROOT / "tests"
SIM = ROOT / "sim"
OUT = ROOT / "docs" / "TEST_ID_MAP.md"

ID_RE = re.compile(r"\bL\d-[A-Z]+-\d+\b")
# A matrix row: | `L3-LINK-05` | role asymmetry on silicon | what it proves | ...
ROW_RE = re.compile(r"^\|\s*`(L\d-[A-Z]+-\d+)`\s*\|\s*([^|]+?)\s*\|")
# A pytest docstring opener: """L3-LINK-01: both dies converge to FCSM=4 ...
DOC_RE = re.compile(r'"""\s*(L\d-[A-Z]+-\d+)\s*:\s*(.+?)\s*$')

# Two descriptions of the same test rarely match word-for-word. Below this
# similarity we treat a shared id as a genuine collision rather than a rename.
SIMILARITY_FLOOR = 0.45

# HUMAN-VERIFIED SAME. The similarity heuristic cannot tell a terse plan name
# ("eth -> compute SRAM") from a verbose implementation summary ("an eth-die
# peer write reaches the COMPUTE die's shared_sram_0") — they score low and get
# flagged as divergent. A divergence list with false positives stops being read,
# which defeats the point, so deliberate mappings are declared here.
#
# ONLY add an id after reading BOTH the matrix row and the test and confirming
# they are the same test. This table is the one place a wrong entry can hide a
# real collision, so it is kept short and auditable.
CONFIRMED_SAME = {
    # The heterogeneous-pair cocotb suite, mapped onto the L0-SIM rows the plan
    # had already specified for exactly these cases (docs/SIM_PLAN.md 9a).
    "L0-SIM-02",  # het link bring-up            <- manual-posture link to FCSM=4
    "L0-SIM-03",  # eth -> compute SRAM          <- peer write lands in compute SRAM
    "L0-SIM-05",  # eth -> compute mailbox       <- mailbox at compute's 0x2A
    "L0-SIM-07",  # CAM-off identity control     <- aperture byte arrives untranslated
    "L0-SIM-08",  # inbound confinement DECERR   <- eth's 0x23 refused by compute
    "L0-SIM-10",  # multi-word burst             <- 8 consecutive words intact
    "L0-SIM-13",  # TX-aperture wedge gate       <- link-down TX access ERRORs
    "L0-SIM-17",  # asymmetric reset ordering    <- far-die-dark, near die survives
    "L0-SIM-15",  # compute decode alias + peer  <- 0x41 confirmed, no 224MB alias
}


def collect_matrix():
    """{id: name} for every id that OWNS a row (not merely mentioned in prose)."""
    out = {}
    for line in MATRIX.read_text().splitlines():
        m = ROW_RE.match(line.strip())
        if m and m.group(1) not in out:
            out[m.group(1)] = m.group(2).strip()
    return out


def collect_pytest():
    """{id: (summary, relpath)} for ids that OWN a test docstring.

    Scans `sim/` as well as `tests/`: the heterogeneous-pair cocotb tests carry
    matrix ids too (the L0-SIM area), and leaving them out made a whole level
    look unimplemented when it is not.
    """
    out = {}
    roots = [d for d in (TESTS, SIM) if d.is_dir()]
    for path in sorted(q for d in roots for q in d.rglob("test_*.py")):
        for line in path.read_text().splitlines():
            m = DOC_RE.search(line.strip())
            if m and m.group(1) not in out:
                out[m.group(1)] = (m.group(2).rstrip("."), str(path.relative_to(ROOT)))
    return out


def similarity(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def render(matrix, code):
    shared = sorted(set(matrix) & set(code))
    divergent = [i for i in shared
                 if i not in CONFIRMED_SAME
                 and similarity(matrix[i], code[i][0]) < SIMILARITY_FLOOR]
    agreeing = [i for i in shared if i not in divergent]
    plan_only = sorted(set(matrix) - set(code))
    code_only = sorted(set(code) - set(matrix))

    L = []
    a = L.append
    a("# Test-id map — plan ↔ implementation")
    a("")
    a("**Generated by `scripts/test_id_map.py`. Do not edit by hand.**")
    a("Regenerate with `make test-id-map`; CI checks it is current.")
    a("")
    a("[`docs/TEST_MATRIX.md`](TEST_MATRIX.md) is the **plan** — one area per")
    a("functional topic, and a superset that includes tests not yet written.")
    a("[`tests/`](../tests/) is the **implementation** — one area per file, per")
    a("[`REPO_LAYOUT.md`](REPO_LAYOUT.md). The two numbering schemes are")
    a("independent: **an id shared by both is not necessarily the same test.**")
    a("")
    a("| | count |")
    a("|---|---:|")
    a(f"| plan ids (matrix rows) | {len(matrix)} |")
    a(f"| implemented ids (pytest) | {len(code)} |")
    a(f"| shared id strings | {len(shared)} |")
    a(f"| — of which **divergent** ⚠️ | **{len(divergent)}** |")
    a(f"| — of which plausibly the same | {len(agreeing)} |")
    a("")

    a("## ⚠️ Divergent — same id, different test")
    a("")
    if divergent:
        a("These ids exist in both namespaces and describe **different things**.")
        a("Never resolve one against the other. Cite the namespace explicitly.")
        a("")
        a("| id | plan says | implementation says | file |")
        a("|---|---|---|---|")
        for i in divergent:
            a(f"| `{i}` | {matrix[i]} | {code[i][0]} | [{code[i][1]}](../{code[i][1]}) |")
    else:
        a("None. 🎉")
    a("")

    a("## Same test in both namespaces")
    a("")
    a("Shared ids that are the same test — either scored similar enough, or")
    a("listed in `CONFIRMED_SAME` in the generator after a human read both.")
    a(f"({len(CONFIRMED_SAME & set(shared))} are human-confirmed, marked ✓.)")
    a("")
    a("| id | plan says | implementation says | file |")
    a("|---|---|---|---|")
    for i in agreeing:
        mark = " ✓" if i in CONFIRMED_SAME else ""
        a(f"| `{i}`{mark} | {matrix[i]} | {code[i][0]} | [{code[i][1]}](../{code[i][1]}) |")
    a("")

    a("## Planned, not implemented")
    a("")
    a(f"{len(plan_only)} matrix ids have no pytest test of that id. Expected —")
    a("the matrix covers levels and blocked items that are not yet written.")
    a("")
    a("| id | plan says |")
    a("|---|---|")
    for i in plan_only:
        a(f"| `{i}` | {matrix[i]} |")
    a("")

    a("## Implemented, not in the matrix")
    a("")
    if code_only:
        a(f"{len(code_only)} pytest ids have no matrix row — the matrix should gain one.")
        a("")
        a("| id | implementation says | file |")
        a("|---|---|---|")
        for i in code_only:
            a(f"| `{i}` | {code[i][0]} | [{code[i][1]}](../{code[i][1]}) |")
    else:
        a("None.")
    a("")
    a("---")
    a("")
    a("*Copyright (C) 2026, SoC Labs (www.soclabs.org)*")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the generated file is missing or stale")
    args = ap.parse_args()

    matrix, code = collect_matrix(), collect_pytest()
    if not matrix:
        print("ERROR: parsed 0 ids from %s — row format changed?" % MATRIX, file=sys.stderr)
        return 2
    if not code:
        print("ERROR: parsed 0 ids from %s — docstring format changed?" % TESTS, file=sys.stderr)
        return 2

    new = render(matrix, code)
    if args.check:
        old = OUT.read_text() if OUT.exists() else ""
        if old != new:
            print("ERROR: %s is stale — run 'make test-id-map'" % OUT.relative_to(ROOT),
                  file=sys.stderr)
            return 1
        print("test-id map current (%d plan, %d implemented)" % (len(matrix), len(code)))
        return 0

    OUT.write_text(new)
    shared = set(matrix) & set(code)
    div = [i for i in sorted(shared) if i not in CONFIRMED_SAME
           and similarity(matrix[i], code[i][0]) < SIMILARITY_FLOOR]
    print("wrote %s — %d plan, %d implemented, %d shared, %d DIVERGENT"
          % (OUT.relative_to(ROOT), len(matrix), len(code), len(shared), len(div)))
    for i in div:
        print("  DIVERGENT %-14s plan=%r  code=%r" % (i, matrix[i], code[i][0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
