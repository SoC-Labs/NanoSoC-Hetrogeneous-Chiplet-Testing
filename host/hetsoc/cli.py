# =============================================================================
# hetsoc.cli — the operator front end.
#
# DESIGN RULE: the DEFAULT of every command is READ-ONLY. `status`, `verify`,
# `health` and `targets` cannot change a board's state; only `bringup --deploy`
# and `recover` do, and both say so before they act. This mirrors
# kr260_eth_bringup.py, whose default action is `--status` for the same reason:
# on this bench the destructive action (re-running the bring-up on a live link)
# looks exactly like the safe one from the command line.
#
# EXIT CODES  0 pass · 1 test/verify failed · 2 usage or config error
#             3 WEDGE — the board needs a JTAG POR before anything else
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""Command-line interface: `python -m hetsoc` / `hetsoc`."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Sequence

from . import __version__, log as _log_mod, regs
from .safety import (ConfigError, HetsocError, LinkDownError, WedgeDetected)
from .targets import get_target, target_names

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_WEDGE = 3


# =============================================================================
# Helpers
# =============================================================================
def _load_config(args: argparse.Namespace):
    from . import config as _config

    return _config.load(args.config)


def _resolve_pair(args: argparse.Namespace):
    cfg = _load_config(args)
    return cfg, cfg.pair(args.pair)


def _selected_boards(args: argparse.Namespace) -> List[Any]:
    cfg, pair = _resolve_pair(args)
    if getattr(args, "board", None):
        wanted = set(args.board)
        chosen = [b for b in pair.boards if b.name in wanted]
        missing = wanted - {b.name for b in chosen}
        if missing:
            raise ConfigError("board(s) %s are not in pair %r (members: %s)"
                              % (", ".join(sorted(missing)), args.pair,
                                 ", ".join(b.name for b in pair.boards)))
        return chosen
    return list(pair.boards)


def _emit(payload: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))


# =============================================================================
# Commands
# =============================================================================
def cmd_targets(args: argparse.Namespace) -> int:
    """Dump the address-descriptor registry. Needs no config and no boards."""
    names = [args.name] if args.name else target_names()
    for name in names:
        target = get_target(name)
        print(target.describe())
        print()
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    """Show the resolved bench description."""
    from . import config as _config

    cfg = _config.load(args.config)
    print(cfg.describe())
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    """READ-ONLY per-board status: backdoor aliveness, role strap, link state.

    The equivalent of `kr260_eth_run.sh status` for both dies at once. Touches
    only combinational boot ROM and RO APB registers, so it cannot wedge.
    """
    boards = _selected_boards(args)
    report: Dict[str, Any] = {"boards": {}}
    ok = True
    for board in boards:
        print("=== %s ===" % board.describe())
        entry: Dict[str, Any] = {}
        try:
            alive = board.alive()
            entry["alive"] = alive
            print("  backdoor boot-ROM probe : %s" % ("ALIVE" if alive else "FAIL"))
            ok &= alive

            role = board.role_status()
            entry["role"] = role
            print("  ROLE_STATUS 0x%08X    : effective_role=%d (%s), locked=%d  "
                  "-> expected %s [%s]"
                  % (role["raw"], role["effective_role"],
                     "master" if role["is_master"] else "slave",
                     role["role_locked"], board.role,
                     "OK" if role["role_ok"] else "MISMATCH"))
            if not role["role_ok"]:
                print("     !! role mismatch — die_a/die_b images may be SWAPPED, "
                      "which also means two drivers on every ribbon lane.")
            ok &= bool(role["role_ok"])

            status = board.lane_status()
            entry["lane_status"] = status.as_dict()
            print("  SWI_LANE_STATUS 0x%08X: fcsm=%d (%s) cal_done=%d "
                  "lane_locked=0x%02X lane_fault=0x%02X cr=%d crack=%d -> %s"
                  % (status.raw, status.fcsm, status.fcsm_name, status.cal_done,
                     status.lane_locked, status.lane_fault, status.cr_seen,
                     status.crack_seen, "UP" if status.link_up else "DOWN"))
        except HetsocError as exc:
            entry["error"] = str(exc)
            ok = False
            print("  ERROR: %s" % exc)
        report["boards"][board.name] = entry
        print()
    report["ok"] = ok
    _emit(report, args.json)
    return EXIT_OK if ok else EXIT_FAIL


def cmd_verify(args: argparse.Namespace) -> int:
    """READ-ONLY: is the link up (FCSM=4, cal_done=1) on BOTH dies?

    The non-destructive alternative to `bringup`. Use this whenever the link may
    already be live — re-running the bring-up on a live link desyncs it.
    """
    _cfg, pair = _resolve_pair(args)
    up = pair.verify_link()
    for board in pair.boards:
        status = board.lane_status()
        print("  %-8s fcsm=%d (%s) cal_done=%d  -> %s"
              % (board.name, status.fcsm, status.fcsm_name, status.cal_done,
                 "UP" if status.link_up else "DOWN"))
    print("RESULT: link is %s bilaterally." % ("UP" if up else "NOT up"))
    if not up:
        print("  If the dies are freshly deployed, run: hetsoc bringup --deploy")
    _emit({"link_up": up}, args.json)
    return EXIT_OK if up else EXIT_FAIL


def cmd_bringup(args: argparse.Namespace) -> int:
    """Bring the link up on BOTH dies concurrently. THIS WRITES TO THE BOARDS."""
    _cfg, pair = _resolve_pair(args)
    print("=== bring-up: %s ===" % pair.describe())
    if not args.deploy and not args.force:
        print("NOTE: without --deploy the dies must already be fresh (post-deploy "
              "or post-POR). Re-running the bring-up on a LIVE link desyncs it "
              "and hangs the sender.")
    try:
        pair.bringup(deploy=args.deploy, force=args.force)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    except LinkDownError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    print("RESULT: LINK UP — FCSM=%d (LINK_IDLE), cal_done=1 on both dies."
          % regs.FCSM_LINK_IDLE)
    _emit({"link_up": True}, args.json)
    return EXIT_OK


def cmd_health(args: argparse.Namespace) -> int:
    """READ-ONLY link + per-node flow-control health.

    The diagnostic that catches the known cross-die wedge: OBS_FC_CREDIT and
    SWI_LANE_STATUS see only the sideband node, NOT the AXI data nodes that
    actually wedge. Poll this BETWEEN cross-die transfers.
    """
    from . import health as _health

    boards = _selected_boards(args)
    report: Dict[str, Any] = {"boards": {}}
    ok = True
    for board in boards:
        sample = _health.link_health(board)
        report["boards"][board.name] = sample
        print("=== health: %s ===" % board.name)
        print(_health.format_health(sample))
        print()
        ok &= bool(sample["verdict"]["ok"])
    report["ok"] = ok
    _emit(report, args.json)
    return EXIT_OK if ok else EXIT_FAIL


def cmd_recover(args: argparse.Namespace) -> int:
    """JTAG POR one or both boards — the ONLY recovery from a wedged PS bus.

    Runs on the fpgahub daemon host, via the documented single-member reset (the
    group `board reset` breaks on the board's `_pl` topology entry).
    """
    boards = _selected_boards(args)
    failures = []
    for board in boards:
        if not board.fpgahub_name:
            print("  %s: no `fpgahub` name configured — cannot POR it." % board.name,
                  file=sys.stderr)
            failures.append(board.name)
            continue
        print("  POR %s (fpgahub=%s) ..." % (board.name, board.fpgahub_name))
        try:
            board.por()
        except HetsocError as exc:
            print("  FAILED: %s" % exc, file=sys.stderr)
            failures.append(board.name)
            continue
        if args.wait and not board.wait_reachable():
            print("  %s did not come back — check power/network." % board.name,
                  file=sys.stderr)
            failures.append(board.name)
    if failures:
        return EXIT_FAIL
    print("RESULT: POR issued. The dies are FRESH — re-deploy, then bring the "
          "link up on BOTH boards together.")
    return EXIT_OK


def cmd_regs(args: argparse.Namespace) -> int:
    """Print the shared register offsets (documentation, no board needed)."""
    print("TLAPB_BASE     = 0x%08X   (per-target; use board.reg(offset))"
          % regs.TLAPB_BASE)
    print("TIDECHART_BASE = 0x%08X" % regs.TIDECHART_BASE)
    print()
    print("offset    name                      absolute (default TLAPB base)")
    for name, value in regs.all_offsets():
        print("  0x%04X  %-24s 0x%08X" % (value, name, regs.TLAPB_BASE + value))
    return EXIT_OK


# =============================================================================
# Parser
# =============================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hetsoc",
        description="Host test framework for the NanoSoC heterogeneous chiplet "
                    "pair (two KR260s joined by a J21 ribbon). Every command "
                    "except `bringup` and `recover` is READ-ONLY.")
    parser.add_argument("--version", action="version",
                        version="hetsoc %s" % __version__)
    parser.add_argument("--config", help="path to hetsoc.toml (default: "
                                         "$HETSOC_CONFIG, ./hetsoc.toml, "
                                         "~/.config/hetsoc.toml)")
    parser.add_argument("--log", default=None,
                        help="log level (DEBUG/INFO/WARNING; env HETSOC_LOG)")
    parser.add_argument("--json", action="store_true",
                        help="also emit a machine-readable result payload")
    sub = parser.add_subparsers(dest="command")

    def add(name: str, func, help_text: str, needs_pair: bool = True,
            needs_board: bool = False):
        cmd = sub.add_parser(name, help=help_text, description=func.__doc__)
        cmd.set_defaults(func=func)
        if needs_pair:
            cmd.add_argument("--pair", default="default",
                             help="pair name from hetsoc.toml (default: default)")
        if needs_board:
            cmd.add_argument("--board", action="append",
                             help="restrict to this board (repeatable)")
        return cmd

    add("status", cmd_status, "read-only per-board status (backdoor, role, link)",
        needs_board=True)
    add("verify", cmd_verify, "read-only: is the link up on BOTH dies?")
    brought = add("bringup", cmd_bringup, "bring the link up on both dies (WRITES)")
    brought.add_argument("--deploy", action="store_true",
                         help="reflash both dies first — the SAFE flow: the link "
                              "is then only brought up on FRESH dies")
    brought.add_argument("--force", action="store_true",
                         help="bring up although the dies are not known-fresh. "
                              "ONLY after confirming read-only that the link is "
                              "DOWN on both dies")
    add("health", cmd_health, "read-only link + per-node FC health (wedge diagnostic)",
        needs_board=True)
    recovered = add("recover", cmd_recover,
                    "JTAG POR a wedged board via fpgahub (WRITES)", needs_board=True)
    recovered.add_argument("--no-wait", dest="wait", action="store_false",
                           default=True,
                           help="do not wait for the board to answer again")

    targets_cmd = sub.add_parser("targets", help="dump the target registry")
    targets_cmd.add_argument("name", nargs="?", help="one target name")
    targets_cmd.set_defaults(func=cmd_targets)

    config_cmd = sub.add_parser("config", help="show the resolved bench config")
    config_cmd.set_defaults(func=cmd_config)

    regs_cmd = sub.add_parser("regs", help="print the shared register offsets")
    regs_cmd.set_defaults(func=cmd_regs)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE

    _log_mod.configure(args.log)
    try:
        return args.func(args)
    except WedgeDetected as exc:
        print("\nWEDGE: %s" % exc, file=sys.stderr)
        print("Recover with: hetsoc recover --board <name>", file=sys.stderr)
        return EXIT_WEDGE
    except ConfigError as exc:
        print("CONFIG ERROR: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    except KeyError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    except HetsocError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:                                # pragma: no cover
        print("\ninterrupted", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":                                   # pragma: no cover
    sys.exit(main())
