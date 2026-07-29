#!/usr/bin/env python3
"""Render a results directory into a single self-contained HTML summary.

The small sibling of TideLink's ``ci/generate_dashboard.py``. It reads the same
inputs as ``results_to_junit.py`` and answers one question at a glance:

    which test LEVELS are green, and how far up the ladder did we actually get?

That framing matters more here than a raw pass count, because the levels are
not independent. L4 skipped is the normal, correct state — it is attended-only
— whereas L3 skipped means the pair never came up and every number below it is
meaningless. The output makes "not run" visually distinct from "passed", so
nobody reads a wall of green and concludes the data plane works.

    dashboard.py <results_dir> [-o dashboard.html] [--markdown out.md]

No external dependencies, no CDN links: one file you can scp or attach to a CI
job. Also prints a plain-text table to stdout, which is usually all you want.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

LEVELS = [
    ("l0", "L0", "offline — host logic, no boards", "always"),
    ("l1l2", "L1/L2", "single board — probes + config plane", "nightly"),
    ("l3", "L3", "pair — link up + control plane", "nightly"),
    ("l4", "L4", "cross-die DATA plane", "attended only"),
    ("l5", "L5", "soak / stress", "attended only"),
]


class Level:
    def __init__(self, key: str, label: str, desc: str, cadence: str) -> None:
        self.key = key
        self.label = label
        self.desc = desc
        self.cadence = cadence
        self.tests = 0
        self.failures = 0
        self.errors = 0
        self.skipped = 0
        self.time = 0.0
        self.present = False

    @property
    def state(self) -> str:
        if not self.present:
            return "not-run"
        if self.errors or self.failures:
            return "fail"
        if self.tests == 0 or self.tests == self.skipped:
            return "empty"
        return "pass"

    @property
    def summary(self) -> str:
        if not self.present:
            return "not run"
        if self.tests == 0:
            return "no cases"
        bits = [f"{self.tests - self.failures - self.errors - self.skipped} passed"]
        if self.failures:
            bits.append(f"{self.failures} failed")
        if self.errors:
            bits.append(f"{self.errors} errored")
        if self.skipped:
            bits.append(f"{self.skipped} skipped")
        return ", ".join(bits)


def collect(results_dir: Path) -> tuple[list[Level], list[tuple[str, str, str, str]]]:
    levels = [Level(*spec) for spec in LEVELS]
    by_key = {lv.key: lv for lv in levels}

    for path in sorted(results_dir.glob("pytest-*.xml")):
        key = path.stem[len("pytest-"):]
        lv = by_key.get(key)
        if lv is None:
            continue
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            lv.present = True
            lv.errors += 1
            continue
        suites = list(root.iter("testsuite")) if root.tag == "testsuites" else [root]
        lv.present = True
        for s in suites:
            for attr, field in (
                ("tests", "tests"),
                ("failures", "failures"),
                ("errors", "errors"),
                ("skipped", "skipped"),
            ):
                try:
                    setattr(lv, field, getattr(lv, field) + int(s.get(attr) or 0))
                except (TypeError, ValueError):
                    pass
            try:
                lv.time += float(s.get("time") or 0.0)
            except (TypeError, ValueError):
                pass

    # Bench-script rows, kept as a flat list — they do not belong to a level.
    bench: list[tuple[str, str, str, str]] = []
    for path in sorted(results_dir.glob("*.json")):
        try:
            doc = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        for row in doc.get("results") or []:
            if not isinstance(row, dict):
                continue
            passed = bool(row.get("ok"))
            gating = bool(row.get("gating", True))
            state = "pass" if passed else ("fail" if gating else "warn")
            bench.append(
                (path.stem, str(row.get("name", "?")), state, str(row.get("detail", "")))
            )
    return levels, bench


def text_table(levels: list[Level], bench: list[tuple[str, str, str, str]]) -> str:
    out = []
    out.append("-" * 78)
    out.append(f"  {'LEVEL':<7}{'STATE':<10}{'CADENCE':<15}{'RESULT'}")
    out.append("-" * 78)
    for lv in levels:
        out.append(f"  {lv.label:<7}{lv.state:<10}{lv.cadence:<15}{lv.summary}")
    out.append("-" * 78)
    if bench:
        out.append(f"  {'BENCH':<20}{'CHECK':<18}{'STATE':<8}{'DETAIL'}")
        out.append("-" * 78)
        for src, name, state, detail in bench:
            out.append(f"  {src:<20}{name:<18}{state:<8}{detail[:28]}")
        out.append("-" * 78)
    return "\n".join(out)


def markdown(levels: list[Level], bench: list[tuple[str, str, str, str]], when: str) -> str:
    icon = {"pass": "PASS", "fail": "FAIL", "empty": "empty", "not-run": "—", "warn": "warn"}
    lines = [
        "# hetsoc test status",
        "",
        f"_generated {when}_",
        "",
        "| Level | State | Cadence | Result | What it covers |",
        "|---|---|---|---|---|",
    ]
    for lv in levels:
        lines.append(
            f"| {lv.label} | {icon.get(lv.state, lv.state)} | {lv.cadence} "
            f"| {lv.summary} | {lv.desc} |"
        )
    if bench:
        lines += ["", "## Bench checks", "",
                  "| Source | Check | State | Detail |", "|---|---|---|---|"]
        for src, name, state, detail in bench:
            lines.append(f"| {src} | {name} | {icon.get(state, state)} | {detail} |")
    lines += [
        "",
        "L4/L5 showing `—` is the expected, correct state: they wedge silicon and",
        "are attended-only. They are never run by CI. See `docs/CI.md`.",
        "",
    ]
    return "\n".join(lines)


CSS = """
:root { color-scheme: light dark; --fg:#111; --bg:#fff; --mut:#666; --line:#ddd;
        --pass:#177245; --fail:#a1201c; --warn:#8a6d00; --none:#777; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e6e6e6; --bg:#151515; --mut:#9a9a9a; --line:#333;
          --pass:#5bbd7f; --fail:#e8746f; --warn:#d9b23a; --none:#888; }
}
body { font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, sans-serif;
       color: var(--fg); background: var(--bg); margin: 0; padding: 2rem 1.25rem; }
main { max-width: 60rem; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
p.meta { color: var(--mut); margin: 0 0 2rem; font-size: .85rem; }
table { border-collapse: collapse; width: 100%; margin-bottom: 2rem; }
th, td { text-align: left; padding: .5rem .6rem; border-bottom: 1px solid var(--line); }
th { font-size: .75rem; text-transform: uppercase; letter-spacing: .06em;
     color: var(--mut); font-weight: 600; }
td.lvl { font-weight: 600; white-space: nowrap; }
td.desc { color: var(--mut); }
.badge { display: inline-block; padding: .1rem .5rem; border-radius: 999px;
         font-size: .75rem; font-weight: 600; border: 1px solid currentColor; }
.pass { color: var(--pass); } .fail { color: var(--fail); }
.warn, .empty { color: var(--warn); } .not-run { color: var(--none); }
.note { border-left: 3px solid var(--warn); padding: .5rem 0 .5rem .9rem;
        color: var(--mut); font-size: .9rem; }
.wrap { overflow-x: auto; }
"""


def render_html(levels: list[Level], bench: list[tuple[str, str, str, str]], when: str) -> str:
    def badge(state: str) -> str:
        text = {"pass": "pass", "fail": "FAIL", "empty": "no cases", "not-run": "not run",
                "warn": "warn"}.get(state, state)
        return f'<span class="badge {state}">{html.escape(text)}</span>'

    rows = "\n".join(
        f"<tr><td class='lvl'>{html.escape(lv.label)}</td>"
        f"<td>{badge(lv.state)}</td>"
        f"<td>{html.escape(lv.cadence)}</td>"
        f"<td>{html.escape(lv.summary)}</td>"
        f"<td class='desc'>{html.escape(lv.desc)}</td></tr>"
        for lv in levels
    )

    bench_block = ""
    if bench:
        brows = "\n".join(
            f"<tr><td>{html.escape(src)}</td><td>{html.escape(name)}</td>"
            f"<td>{badge(state)}</td><td class='desc'>{html.escape(detail)}</td></tr>"
            for src, name, state, detail in bench
        )
        bench_block = (
            "<h2>Bench checks</h2><div class='wrap'><table>"
            "<tr><th>Source</th><th>Check</th><th>State</th><th>Detail</th></tr>"
            f"{brows}</table></div>"
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hetsoc test status</title><style>{CSS}</style></head>
<body><main>
<h1>NanoSoC heterogeneous chiplet &mdash; test status</h1>
<p class="meta">generated {html.escape(when)}</p>
<div class="wrap"><table>
<tr><th>Level</th><th>State</th><th>Cadence</th><th>Result</th><th>Covers</th></tr>
{rows}
</table></div>
{bench_block}
<p class="note">L4 and L5 showing <em>not run</em> is the expected state, not a
gap in coverage. The cross-die data plane intermittently wedges current silicon,
so those levels are attended-only and are never run by CI.</p>
</main></body></html>
"""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("results_dir", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None, help="HTML output path")
    ap.add_argument("--markdown", type=Path, default=None, help="also write a markdown summary")
    args = ap.parse_args(argv[1:])

    if not args.results_dir.is_dir():
        print(f"dashboard: no such directory: {args.results_dir}", file=sys.stderr)
        return 2

    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    levels, bench = collect(args.results_dir)

    print(text_table(levels, bench))

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(render_html(levels, bench, when))
        print(f"dashboard: wrote {args.output}")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown(levels, bench, when))
        print(f"dashboard: wrote {args.markdown}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
