# =============================================================================
# hetsoc.fpgahub — bench resource control: lease, status, and JTAG POR recovery.
#
# TWO DOCUMENTED QUIRKS, BOTH HANDLED IN CODE
# -------------------------------------------
# 1. PER-BOARD ENDPOINTS 404 FROM THIS HOST.
#    `fpgahub board reset` and the other `board/<name>/…` routes 404 from some
#    client hosts (a CLI/daemon route skew) but work when run ON the host where
#    the daemon lives. The COLLECTION endpoints (`status`, `board list`,
#    `lease …`) work from anywhere. So this module routes per-board calls
#    through `ssh mapstone-dev` and keeps collection calls local.
#    (KR260_BENCH_RUNBOOK.md §2 "Recovery gotcha", verified 2026-07-27.)
#
# 2. THE GROUP `board reset` BREAKS ON THE `_pl` TOPOLOGY ENTRY.
#    A KR260 appears twice in the topology (the PS entry via the switch and the
#    `_pl` entry behind the USB dongle), and the group reset trips over the
#    second. The documented workaround is a SINGLE-MEMBER reset posted straight
#    at the daemon's unix socket (KR260_BENCH_RUNBOOK.md §10). That curl form is
#    the default here; the CLI form is the fallback.
#
# WHY THIS MATTERS MORE THAN IT LOOKS
# -----------------------------------
# A JTAG POR is the ONLY recovery from a wedged PS AXI bus. If `reset` does not
# work, a wedged board stays wedged and the bench session is over. So the
# recovery path gets the belt-and-braces treatment, not the lease path.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""fpgahub board leasing, status and JTAG-POR recovery."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .log import get_logger
from .safety import HetsocError, WedgeDetected

__all__ = ["lease", "acquire", "release", "reset", "status", "board_show",
           "run_action", "FpgahubError", "HUB_HOST", "run_local"]

_log = get_logger("fpgahub")


class FpgahubError(HetsocError):
    """An fpgahub command failed."""


#: The host the fpgahub daemon lives on. Per-board endpoints must run there.
HUB_HOST = os.environ.get("HETSOC_FPGAHUB_HOST", "mapstone-dev")

#: Daemon socket + API prefix used by the single-member reset workaround.
HUB_SOCKET = os.environ.get("HETSOC_FPGAHUB_SOCKET", "/run/fpgahub/fpgahub.sock")
HUB_API = "http://localhost/api/v1"

_DEFAULT_TIMEOUT_S = 120.0


# =============================================================================
# Command plumbing
# =============================================================================
def run_local(command, timeout_s: float = _DEFAULT_TIMEOUT_S,
              shell: bool = False, check: bool = True) -> str:
    """Run a command on THIS host and return stdout."""
    argv = command if shell else list(command)
    printable = command if shell else " ".join(shlex.quote(a) for a in argv)
    _log.debug("local", command=printable)
    try:
        done = subprocess.run(argv, shell=shell, timeout=timeout_s,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        raise WedgeDetected("command timed out after %.0fs: %s"
                            % (timeout_s, printable))
    except OSError as exc:
        raise FpgahubError("could not run %s: %s" % (printable, exc))
    out = done.stdout.decode(errors="replace")
    if check and done.returncode != 0:
        raise FpgahubError(
            "command failed (rc=%d): %s\n%s%s"
            % (done.returncode, printable, out,
               done.stderr.decode(errors="replace")))
    return out


def run_on_hub_host(remote_command: str,
                    timeout_s: float = _DEFAULT_TIMEOUT_S,
                    check: bool = True) -> str:
    """Run a command on the fpgahub daemon host (default ``mapstone-dev``).

    Set ``HETSOC_FPGAHUB_LOCAL=1`` when running ON that host already — then the
    ssh hop is skipped.
    """
    if os.environ.get("HETSOC_FPGAHUB_LOCAL") == "1":
        return run_local(remote_command, timeout_s, shell=True, check=check)
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            HUB_HOST, remote_command]
    return run_local(argv, timeout_s, check=check)


# =============================================================================
# Collection endpoints — work from any host
# =============================================================================
def status() -> Dict[str, Any]:
    """Board inventory + lease state, as fpgahub's ``/status`` payload.

    Collection endpoint, so it runs locally; falls back to the daemon host if the
    local CLI cannot reach the socket.
    """
    for runner, where in ((lambda: run_local(["fpgahub", "status", "--json"]), "local"),
                          (lambda: run_on_hub_host("fpgahub status --json"), HUB_HOST)):
        try:
            raw = runner()
        except (FpgahubError, WedgeDetected) as exc:
            _log.debug("status failed", where=where, error=str(exc))
            continue
        try:
            return json.loads(raw)
        except ValueError:
            _log.debug("status returned non-JSON", where=where)
            continue
    raise FpgahubError(
        "could not read fpgahub status locally or on %s. Is fpgahub installed "
        "and is fpgahubd running? (`fpgahub status` should list kr260_01 / "
        "kr260_02.)" % HUB_HOST)


def acquire(board_name: str, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
    """Acquire the lease on one board (collection endpoint — runs locally)."""
    _log.info("lease acquire", board=board_name)
    run_local(["fpgahub", "lease", "acquire", board_name], timeout_s)


def release(board_name: str, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
    """Release the lease on one board. Best-effort: never masks a test failure."""
    try:
        run_local(["fpgahub", "lease", "release", board_name], timeout_s)
        _log.info("lease release", board=board_name)
    except (FpgahubError, WedgeDetected) as exc:
        _log.warning("lease release failed — release it by hand",
                     board=board_name, error=str(exc))


@contextmanager
def lease(*board_names: str) -> Iterator[List[str]]:
    """Hold leases on *board_names* for the duration of the block.

    ::

        with hetsoc.fpgahub.lease("kr260_01", "kr260_02"):
            pair.verify_link()

    Leases are released in reverse order on the way out, including on exception —
    a bench that keeps a lease after a crashed run blocks everyone else.
    """
    held: List[str] = []
    try:
        for name in board_names:
            acquire(name)
            held.append(name)
        yield list(held)
    finally:
        for name in reversed(held):
            release(name)


# =============================================================================
# Per-board endpoints — MUST run on the daemon host (they 404 from here)
# =============================================================================
def board_show(board_name: str) -> str:
    """`fpgahub board show <name>` — per-board, so it runs on the daemon host."""
    return run_on_hub_host("fpgahub board show %s" % shlex.quote(board_name))


def run_action(board_name: str, action: str,
               timeout_s: float = 600.0) -> str:
    """`fpgahub actions run <board> <action>` — e.g. the role-aware deploy.

    Per-board endpoint, so it runs on the daemon host.
    """
    _log.info("fpgahub action", board=board_name, action=action)
    return run_on_hub_host(
        "fpgahub actions run %s %s" % (shlex.quote(board_name), shlex.quote(action)),
        timeout_s=timeout_s)


def _curl_reset_command(board_name: str) -> str:
    """The documented SINGLE-MEMBER reset (KR260_BENCH_RUNBOOK.md §10).

    Posted straight at the daemon's unix socket because the group ``board reset``
    breaks on the board's ``_pl`` topology entry.
    """
    payload = json.dumps({"method": "default", "confirm": True})
    return ("curl -sS --fail-with-body --unix-socket %s -X POST %s/targets/%s/reset "
            "-H 'Content-Type: application/json' -d %s"
            % (shlex.quote(HUB_SOCKET), HUB_API,
               shlex.quote(board_name), shlex.quote(payload)))


def reset(board_name: str, method: str = "auto",
          timeout_s: float = 120.0) -> None:
    """Issue a JTAG power-on reset — the ONLY recovery from a wedged PS bus.

    Runs on the fpgahub daemon host (``mapstone-dev``) because the per-board
    routes 404 from other clients. Tries the single-member curl form first (the
    group ``board reset`` breaks on the ``_pl`` topology entry), then the CLI.

    The board answers ping again roughly 10 s later; call
    ``Board.wait_reachable()`` before touching it.

    Args:
        method: ``"auto"`` (curl then CLI), ``"curl"``, or ``"cli"``.
    """
    if method not in ("auto", "curl", "cli"):
        raise ValueError("reset method must be auto|curl|cli, got %r" % method)

    attempts: List[tuple] = []
    if method in ("auto", "curl"):
        attempts.append(("single-member curl", _curl_reset_command(board_name)))
    if method in ("auto", "cli"):
        attempts.append(("fpgahub CLI",
                         "fpgahub board reset %s --yes" % shlex.quote(board_name)))

    errors: List[str] = []
    for label, command in attempts:
        _log.warning("issuing JTAG POR", board=board_name, via=label, host=HUB_HOST)
        try:
            out = run_on_hub_host(command, timeout_s=timeout_s)
        except (FpgahubError, WedgeDetected) as exc:
            errors.append("%s: %s" % (label, exc))
            _log.warning("POR attempt failed", via=label, error=str(exc))
            continue
        _log.info("JTAG POR issued", board=board_name, via=label,
                  reply=(out or "").strip()[:200])
        return

    raise FpgahubError(
        "could not POR %s. A JTAG POR is the only recovery from a wedged PS AXI "
        "bus, so this must be fixed before the bench can continue.\n"
        "  Attempts:\n    %s\n"
        "  Run it by hand on the daemon host:\n"
        "    ssh %s \"%s\"\n"
        "  (Per-board endpoints 404 from other client hosts, and the GROUP "
        "`board reset` breaks on the board's `_pl` topology entry — hence the "
        "single-member form. KR260_BENCH_RUNBOOK.md §2/§10.)"
        % (board_name, "\n    ".join(errors), HUB_HOST,
           _curl_reset_command(board_name)))


def free_boards(names: Optional[Sequence[str]] = None) -> Dict[str, bool]:
    """``{board: is_free}`` from ``status()`` — the pre-flight check."""
    payload = status()
    boards = payload.get("boards") or payload.get("targets") or []
    out: Dict[str, bool] = {}
    for entry in boards:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("id")
        if name is None or (names is not None and name not in names):
            continue
        lease_info = entry.get("lease") or entry.get("in_use")
        out[name] = not bool(lease_info)
    return out
