#!/usr/bin/env python3
"""Merge everything in a results directory into one JUnit XML file.

Modelled on TideLink's ``ci/fpga_runs_to_junit.py``, which turns an fpgahub
``actions run --json`` bundle into JUnit. Same job, different inputs: here the
producers are pytest (already JUnit) and the bench scripts (JSON), and the
consumer is a CI job that wants exactly one file.

Consumes ``<results_dir>``:

    pytest-l0.xml         one per level, written by scripts/run_pytest.sh
    pytest-l1l2.xml
    pytest-l3.xml   ...
    *.json                bench-script summaries, either of two shapes:
                            {"pass": bool, "results": [{name, ok, detail, gating}]}
                            {"results": [...]}          (pass inferred)

and writes a single ``<testsuites>``.

WHY THE JSON SHAPE IS THE ONE IT IS: it is the shape TideLink's
``kr260_eth_regress.py --json`` already emits. Anything that already speaks it
drops straight into this pipeline with no adapter.

NON-GATING RESULTS ARE NOT FAILURES. A bench check with ``gating: false`` that
did not pass becomes ``<skipped type="non-gating">``, not ``<failure>``. On
current silicon the cross-die data plane fails intermittently for a known
reason (recovery-stripped AXI FCSMs); recording that as a hard failure every
night trains everyone to ignore the dashboard, which is worse than not having
one. It is still visible — just not red.

Usage:
    results_to_junit.py <results_dir> [-o out.xml]
    results_to_junit.py <results_dir> --strict     # non-gating counts as failure
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Level -> a human name for the suite, so a CI UI groups sensibly.
LEVEL_NAMES = {
    "l0": "L0-offline",
    "l1l2": "L1L2-single-board",
    "l3": "L3-pair-control-plane",
    "l4": "L4-cross-die-data-plane",
    "l5": "L5-soak",
}


def _int(elem: ET.Element, attr: str) -> int:
    try:
        return int(elem.get(attr) or 0)
    except (TypeError, ValueError):
        return 0


def _float(elem: ET.Element, attr: str) -> float:
    try:
        return float(elem.get(attr) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def suites_from_pytest_xml(path: Path) -> list[ET.Element]:
    """Lift the <testsuite> elements out of a pytest --junitxml file.

    pytest writes <testsuites><testsuite/></testsuites>; older versions wrote a
    bare <testsuite>. Handle both rather than assuming, because the venv's
    pytest version is not pinned by this repo.
    """
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        bad = ET.Element(
            "testsuite",
            {"name": f"unparsable.{path.name}", "tests": "1", "errors": "1"},
        )
        tc = ET.SubElement(
            bad, "testcase", {"classname": "junit-merge", "name": path.name, "time": "0"}
        )
        ET.SubElement(tc, "error", {"type": "parse_error", "message": str(exc)})
        return [bad]

    found = list(root.iter("testsuite")) if root.tag == "testsuites" else [root]

    # Name the suite after the level so the CI UI is readable. The filename is
    # the only place that information exists — pytest names every suite
    # "pytest".
    stem = path.stem
    level = stem.removeprefix("pytest-")
    label = LEVEL_NAMES.get(level, level)
    for s in found:
        s.set("name", label)
    return found


def suite_from_bench_json(path: Path, strict: bool) -> ET.Element | None:
    """Turn a bench-script JSON summary into a <testsuite>."""
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict) or "results" not in doc:
        return None

    rows = doc.get("results") or []
    suite = ET.Element("testsuite", {"name": f"bench.{path.stem}"})
    n_tests = n_fail = n_skip = 0

    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "unnamed"))
        passed = bool(row.get("ok"))
        gating = bool(row.get("gating", True))
        detail = str(row.get("detail", ""))

        tc = ET.SubElement(
            suite,
            "testcase",
            {"classname": f"bench.{path.stem}", "name": name, "time": "0"},
        )
        n_tests += 1
        if passed:
            pass
        elif gating or strict:
            ET.SubElement(tc, "failure", {"type": "bench", "message": detail})
            n_fail += 1
        else:
            # Known-flaky-on-silicon. Visible, not red. See the module docstring.
            ET.SubElement(tc, "skipped", {"type": "non-gating", "message": detail})
            n_skip += 1
        if detail:
            out = ET.SubElement(tc, "system-out")
            out.text = detail

    suite.set("tests", str(n_tests))
    suite.set("failures", str(n_fail))
    suite.set("errors", "0")
    suite.set("skipped", str(n_skip))
    return suite


def build(results_dir: Path, strict: bool) -> ET.Element:
    root = ET.Element("testsuites")
    suites: list[ET.Element] = []

    # Deterministic order: L0 first, then by level, then bench JSON.
    order = list(LEVEL_NAMES)
    xmls = sorted(
        (p for p in results_dir.glob("pytest-*.xml")),
        key=lambda p: (
            order.index(p.stem.removeprefix("pytest-"))
            if p.stem.removeprefix("pytest-") in order
            else 99,
            p.name,
        ),
    )
    for path in xmls:
        suites.extend(suites_from_pytest_xml(path))

    for path in sorted(results_dir.glob("*.json")):
        suite = suite_from_bench_json(path, strict)
        if suite is not None:
            suites.append(suite)

    if not suites:
        # An empty results dir is a harness problem, not a green build. Say so
        # in-band, where CI will actually surface it.
        empty = ET.Element(
            "testsuite", {"name": "no-results", "tests": "1", "errors": "1"}
        )
        tc = ET.SubElement(
            empty, "testcase", {"classname": "junit-merge", "name": "harness", "time": "0"}
        )
        ET.SubElement(
            tc,
            "error",
            {
                "type": "no_results",
                "message": f"no pytest-*.xml or *.json found in {results_dir}",
            },
        )
        suites = [empty]

    tests = sum(_int(s, "tests") for s in suites)
    fails = sum(_int(s, "failures") for s in suites)
    errs = sum(_int(s, "errors") for s in suites)
    skips = sum(_int(s, "skipped") for s in suites)
    root.set("name", "hetsoc")
    root.set("tests", str(tests))
    root.set("failures", str(fails))
    root.set("errors", str(errs))
    root.set("skipped", str(skips))
    root.set("time", f"{sum(_float(s, 'time') for s in suites):.3f}")
    for s in suites:
        root.append(s)
    return root


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("results_dir", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument(
        "--strict",
        action="store_true",
        help="count non-gating bench failures as real failures",
    )
    args = ap.parse_args(argv[1:])

    if not args.results_dir.is_dir():
        print(f"results_to_junit: no such directory: {args.results_dir}", file=sys.stderr)
        return 2

    root = build(args.results_dir, args.strict)
    if hasattr(ET, "indent"):
        ET.indent(ET.ElementTree(root), space="  ")
    xml = ET.tostring(root, encoding="unicode")
    text = '<?xml version="1.0" encoding="utf-8"?>\n' + xml + "\n"

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
        print(
            "results_to_junit: {} tests, {} failures, {} errors, {} skipped -> {}".format(
                root.get("tests"),
                root.get("failures"),
                root.get("errors"),
                root.get("skipped"),
                args.output,
            )
        )
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
