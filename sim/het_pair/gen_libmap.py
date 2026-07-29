#!/usr/bin/env python3
"""Emit a Verilog library map + config so the two chiplet dies can share one
simulation without their generated SoC RTL colliding.

Copyright 2026, SoC Labs (www.soclabs.org)

WHY THIS EXISTS
---------------
The ethernet SoC and the compute SoC are rendered by the same generator from
different system descriptions. They therefore contain many modules with the
SAME NAME and DIFFERENT CONTENT. Verilog has one global module namespace, so
concatenating both flists into one compilation makes the netlist a property of
declaration ORDER, not of the design. The collisions are not cosmetic — see
docs/SIM_PLAN.md for the full list; the two that decide it are:

  * `PHC_AHB` — the ethernet SoC's definition has six outputs the compute SoC's
    does not (`seconds_o`, `nanoseconds_o`, `sub_nanoseconds_o`,
    `ha1588_servo_en_o`, `sync_interval_o`, `pps_out`). Whichever loses, the
    other die's wrapper binds ports that do not exist — a hard elaboration
    error, not a warning.

  * `nanosoc_ss_cpu_plus` — the ethernet copy drives
    `system_hreadyout = cpu_0_hready`; the compute copy ties it `1'b1`. That net
    IS the `eth_ss_0` passthrough's HREADY. If the compute copy wins, the
    ethernet die's external stimulus port silently stops honouring wait states
    and every transaction in this testbench becomes untrustworthy. This one is
    SILENT, which is precisely why order-dependence is unacceptable.

Both tools that matter support per-instance library binding, and the liblist of
an instance is inherited by its whole subtree (IEEE 1364-2001 s13.3). So we give
each die its own library and bind it at its instance. Shared read-only vendor IP
(CMSDK / DAP / DMA-250 / tech cells, identical content in both trees) goes into a
third library both dies can see, so it is compiled exactly once.

This also resolves the SECOND divergence: the two repos pin DIFFERENT TideLink
commits (33 differing RTL files, including the FCSM, the GPIO PHY and the replay
CDC). Each die gets the TideLink revision its own repo was verified against,
which is a more faithful model of two chiplets taped out at different times than
forcing both onto one revision would be.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path


def read_flist(path: Path, subs: dict[str, str]) -> tuple[list[str], list[str]]:
    """Split a flattened flist into (source files, compiler switches).

    `subs` maps ${VAR} -> value and is applied BEFORE os.path.expandvars.

    This matters more than it looks. `resolve_tidelink_flist.py` deliberately
    passes `+incdir+` switches through UNEXPANDED (e.g.
    `+incdir+${TIDELINK_HOME}/deps/tidelink-gpio-phy/rtl`), because the chiplet
    Makefiles export TIDELINK_HOME into VCS's own environment. Here there are
    TWO TideLink checkouts at two different commits, so a single ambient
    TIDELINK_HOME would silently give one die the other's include path — and
    `+incdir` is global to a VCS compilation, with no per-library scoping. So we
    resolve each side's variables to that side's tree at generation time, and
    emit both include paths explicitly.
    """
    files: list[str] = []
    switches: list[str] = []

    def apply(tok: str) -> str:
        for k, v in subs.items():
            tok = tok.replace("${%s}" % k, v).replace("$(%s)" % k, v)
        return os.path.expandvars(tok)

    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("//", "#")):
            continue
        if line.startswith(("-", "+")):
            switches.append(apply(line))
            continue
        p = apply(line)
        if os.path.isfile(p):
            files.append(os.path.realpath(p))
    return files, switches


def digest(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--eth-soc", required=True)
    ap.add_argument("--eth-tl", required=True)
    ap.add_argument("--eth-tidechart", required=True)
    ap.add_argument("--eth-rtl", required=True, help="ethernet chiplet src/rtl dir")
    ap.add_argument("--cmp-soc", required=True)
    ap.add_argument("--cmp-tl", required=True)
    ap.add_argument("--cmp-tidechart", required=True)
    ap.add_argument("--cmp-rtl", required=True, help="compute chiplet src/rtl dir")
    ap.add_argument("--eth-home", required=True, help="ethernet chiplet repo root")
    ap.add_argument("--cmp-home", required=True, help="compute chiplet repo root")
    ap.add_argument("--out-libmap", required=True)
    ap.add_argument("--out-config", required=True)
    ap.add_argument("--out-switches", required=True)
    ap.add_argument("--out-sources", required=True)
    args = ap.parse_args()

    eth_subs = {
        "TIDELINK_HOME": os.path.join(args.eth_home, "tidelink"),
        "TIDECHART_HOME": os.path.join(args.eth_home, "tidechart"),
        "NANOSOC_ETH_CHIPLET_HOME": args.eth_home,
        "NANOSOC_MULTICORE_HOME": os.path.join(args.eth_home, "nanosoc-multicore-system"),
    }
    cmp_subs = {
        "TIDELINK_HOME": os.path.join(args.cmp_home, "tidelink"),
        "TIDECHART_HOME": os.path.join(args.cmp_home, "tidechart"),
        "NANOSOC_COMPUTE_CHIPLET_HOME": args.cmp_home,
        "NANOSOC_COMPUTE_HOME": os.path.join(args.cmp_home, "nanosoc-compute-system"),
    }

    eth_files: list[str] = []
    eth_sw: list[str] = []
    cmp_files: list[str] = []
    cmp_sw: list[str] = []
    for f in (args.eth_soc, args.eth_tl, args.eth_tidechart):
        a, b = read_flist(Path(f), eth_subs)
        eth_files += a
        eth_sw += b
    for f in (args.cmp_soc, args.cmp_tl, args.cmp_tidechart):
        a, b = read_flist(Path(f), cmp_subs)
        cmp_files += a
        cmp_sw += b

    # The integration RTL of each repo (chiplet_d2d_decode, tidechart_shim, the
    # chiplet top). chiplet_d2d_decode.sv and tidechart_shim.sv are BYTE-IDENTICAL
    # between the two repos today, but they are still per-die sources: if one repo
    # ever re-derives its decoder for its own window base (which the compute repo
    # must — see SIM_PLAN.md), the two must not collide.
    for d, dst in ((args.eth_rtl, eth_files), (args.cmp_rtl, cmp_files)):
        for name in sorted(os.listdir(d)):
            if name.endswith((".v", ".sv")):
                dst.append(os.path.realpath(os.path.join(d, name)))

    eth_set = dict.fromkeys(eth_files)   # order-preserving unique
    cmp_set = dict.fromkeys(cmp_files)

    # A file is "common" when BOTH dies compile a file of the same basename AND
    # the two files have identical content. Those are the shared read-only vendor
    # cells; compiling them once in a shared library keeps one definition and
    # avoids a spurious duplicate. Anything whose content differs stays private to
    # its die — that is the whole point.
    by_base_eth: dict[str, str] = {}
    for p in eth_set:
        by_base_eth.setdefault(os.path.basename(p), p)
    common: set[str] = set()
    cmp_common_twin: dict[str, str] = {}
    for p in cmp_set:
        b = os.path.basename(p)
        e = by_base_eth.get(b)
        if e and digest(e) == digest(p):
            common.add(e)
            cmp_common_twin[p] = e

    eth_only = [p for p in eth_set if p not in common]
    cmp_only = [p for p in cmp_set if p not in cmp_common_twin]
    common_files = [p for p in eth_set if p in common]

    # ---- library map -----------------------------------------------------
    lm = [
        "// Generated by sim/het_pair/gen_libmap.py - do not edit.",
        "// One library per die + a shared library for identical vendor IP.",
        "",
    ]

    def emit(libname: str, files: list[str]) -> None:
        if not files:
            return
        lm.append(f"library {libname}")
        for i, p in enumerate(files):
            sep = "," if i < len(files) - 1 else ";"
            lm.append(f'    "{p}"{sep}')
        lm.append("")

    emit("common_lib", common_files)
    emit("eth_lib", eth_only)
    emit("cmp_lib", cmp_only)
    Path(args.out_libmap).write_text("\n".join(lm) + "\n")

    # ---- config ----------------------------------------------------------
    # `instance ... liblist` is inherited by the whole subtree below that
    # instance, so binding the two die instances is enough to partition the
    # entire design. The testbench itself and the shared PHY/flash models live in
    # `work` and see both plus common.
    cfg = """// Generated by sim/het_pair/gen_libmap.py - do not edit.
//
// Binds each die instance to its own library. Per IEEE 1364-2001 s13.3 the
// liblist of an instance is INHERITED by every instance below it, so these two
// rules partition both SoC hierarchies completely: u_dieE's subtree resolves
// PHC_AHB, nanosoc_ss_cpu_plus, tidelink_top, ... out of eth_lib, and u_dieC's
// out of cmp_lib.
//
// A liblist is an ORDERED search: first library holding the cell wins. The order
// below is what makes the partition both correct and complete:
//
//   1. <own>_lib     the die's own copy. Listed first, so for every one of the
//                    colliding module names the die ALWAYS gets its own version.
//                    This is the entire point of the exercise.
//   2. common_lib    files byte-identical in both trees (shared vendor IP).
//                    Only one version exists, so there is nothing to get wrong.
//   3. work          cells VCS resolved from `-y` search directories rather than
//                    from an explicit flist entry — the Cortex-M0+ IP
//                    (CORTEXM0PLUS, cm0p_wic, cm0p_ik_pmu, ...) arrives this
//                    way. Those live in the read-only lab IP tree, are shared by
//                    both SoCs, and exist in exactly one version.
//   4. <other>_lib   last resort: a module this die's flist does not list at all
//                    (e.g. cmsdk_ahb_to_apb_ipc, which only the ethernet flist
//                    names explicitly). Reachable only for NON-colliding names,
//                    because any colliding name was already resolved at step 1.
config cfg_het_pair;
    design work.tb_het_pair;
    default liblist work common_lib eth_lib cmp_lib;
    instance tb_het_pair.u_dieE liblist eth_lib common_lib work cmp_lib;
    instance tb_het_pair.u_dieC liblist cmp_lib common_lib work eth_lib;
endconfig
"""
    Path(args.out_config).write_text(cfg)

    # ---- switches (incdirs etc.) ----------------------------------------
    # +incdir+ and +define+ are global to the compilation; collect the union.
    seen: set[str] = set()
    sw_out: list[str] = []
    for s in eth_sw + cmp_sw:
        if s not in seen:
            seen.add(s)
            sw_out.append(s)
    Path(args.out_switches).write_text("\n".join(sw_out) + "\n")

    # ---- source list ----------------------------------------------------
    # VCS's -libmap ASSIGNS files to libraries; it does not by itself cause them
    # to be read. The files must ALSO reach the compiler. (Verified empirically:
    # -libmap alone gives Error-[CFCILFBI] "Cannot find cell in liblist" for
    # every cell; -libmap plus the same files on the command line binds
    # correctly, and demonstrably gives two same-named modules to two different
    # instances.) So emit the union exactly once per path — the 39 colliding
    # basenames are DISTINCT paths, so both copies are compiled and the libmap
    # decides which instance sees which.
    all_sources = common_files + eth_only + cmp_only
    Path(args.out_sources).write_text("\n".join(all_sources) + "\n")

    print(f"gen_libmap: common_lib={len(common_files)} files "
          f"eth_lib={len(eth_only)} cmp_lib={len(cmp_only)} "
          f"sources={len(all_sources)} switches={len(sw_out)}", file=sys.stderr)

    # Report the collisions we actually resolved — this is the evidence the
    # partition was necessary, and it belongs in the build log.
    collisions = set()
    for p in cmp_set:
        b = os.path.basename(p)
        e = by_base_eth.get(b)
        if e and digest(e) != digest(p):
            collisions.add(b)
    if collisions:
        print(f"gen_libmap: {len(collisions)} name collisions with DIFFERING "
              f"content, kept private per die:", file=sys.stderr)
        for b in sorted(collisions):
            print(f"    {b}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
