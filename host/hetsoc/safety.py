# =============================================================================
# hetsoc.safety — the hazard model, enforced in code.
#
# WHY THIS MODULE EXISTS
# ----------------------
# Three failure modes on this bench are *unrecoverable in software*. Each one
# has cost a bench session, and each one is a `raise` here rather than a warning
# in a runbook:
#
#   1. OUT-OF-WINDOW PS ACCESS -> AddressGuardError.
#      The KR260 PS reaches the chiplet SoC only through a narrow backdoor
#      window. Any PS read of a PL address the SoC does not decode is an AXI
#      transaction with NO RESPONDER and NO TIMEOUT: the ZynqMP PS AXI bus hangs
#      hard, the board drops to 100% packet loss, and only a JTAG POR recovers
#      it. This is not hypothetical — the bare-link AFI canaries (0x8403_xxxx)
#      wedged kr260_01 on first load (docs KR260_BENCH_RUNBOOK.md §3).
#      Enforcement lives in `hetsoc.targets.Target.to_host()`.
#
#   2. PEER ACCESS ON A DOWN LINK -> LinkDownError.
#      A write/read into the 0x2F peer aperture traverses the die-to-die link.
#      With the link down the transaction never completes and hangs the PS bus
#      the same way. `kr260_eth_xfer.py:_require_link()` is the proven guard;
#      `require_link_up()` is its packaged form, and `hetsoc.board.Board`
#      calls it on EVERY peer-aperture access, not just at the top of a script.
#
#   3. A HANG IS NOT AN ERROR -> WedgeDetected.
#      The failures above manifest as a call that never returns, so any board
#      operation that is not time-bounded turns a wedged board into a wedged
#      test runner. `guarded()` bounds every one of them.
#
# There is deliberately no "unsafe" escape hatch in this module. The only way
# to reach /dev/mem is through a Target whose window has been declared, and the
# on-board agent re-checks the window a second time (hetsoc.agent).
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""Wedge-hazard exceptions and the guards that enforce them."""

from __future__ import annotations

import functools
import threading
from typing import Any, Callable, TypeVar

__all__ = [
    "HetsocError",
    "AddressGuardError",
    "ProvisionalTargetError",
    "LinkDownError",
    "WedgeDetected",
    "ConfigError",
    "TransportError",
    "require_link_up",
    "guarded",
    "run_guarded",
    "DEFAULT_TIMEOUT_S",
]

F = TypeVar("F", bound=Callable[..., Any])

#: Default wall-clock bound for a single board operation. One 32-bit poke over
#: a warm SSH channel is milliseconds; anything past this is a hang, not slow.
DEFAULT_TIMEOUT_S = 20.0


class HetsocError(Exception):
    """Base class for every error this framework raises deliberately."""


class AddressGuardError(HetsocError):
    """An address was refused before it could reach the board.

    Raised by ``Target.to_host()`` for anything outside the declared backdoor
    window, and by the registry for any address in a known-undecoded (bare-link)
    range. NEVER catch this and retry with a different base — the whole point is
    to stop before an undecoded access hangs the PS AXI bus.
    """


class ProvisionalTargetError(AddressGuardError):
    """The target descriptor exists but carries no *verified* host window.

    The compute chiplet has no FPGA/KR260 port yet, so there is no bitstream and
    therefore no known PS-visible window base. Rather than guess one — a guessed
    base on ZynqMP is an unrecoverable bus hang — the descriptor refuses to
    produce host addresses at all. Subclasses ``AddressGuardError`` so callers
    that guard on the base class still stop.
    """


class LinkDownError(HetsocError):
    """A die-to-die (peer-aperture) access was attempted with FCSM != 4.

    See ``require_link_up``. The link must be bilaterally up (FCSM=4 LINK_IDLE,
    cal_done=1) before anything crosses it.
    """


class WedgeDetected(HetsocError):
    """A board operation exceeded its timeout — treat the board as wedged.

    Recovery is a JTAG POR (``hetsoc.fpgahub.reset`` / ``Board.por()``); a
    software retry will not help and a second access may wedge the *host* tool
    as well.
    """


class ConfigError(HetsocError):
    """Malformed / missing hetsoc.toml, or a board that names no known target."""


class TransportError(HetsocError):
    """The SSH channel or the on-board agent failed (not a bus hang)."""


# =============================================================================
# Guards
# =============================================================================
def require_link_up(board: Any) -> None:
    """Raise ``LinkDownError`` unless *board* reports a bilaterally-up link.

    "Up" is FCSM == 4 (LINK_IDLE) **and** calibration_done == 1, read from
    ``SWI_LANE_STATUS`` — the criterion proven on silicon 2026-07-27 and the one
    ``kr260_eth_xfer.py:_require_link()`` uses. Judge link health by FCSM, never
    by lane-lock (lane_locked self-deasserts to 0x00 after training) and never
    by ``link_active`` (it is literally ``assign link_active = role_locked_o``,
    docs/STATUS_REGISTERS.md §3).

    Called by ``Board.read``/``Board.write`` on every peer-aperture access, so
    there is no code path that peer-accesses a down link.
    """
    status = board.lane_status()
    if not status.link_up:
        raise LinkDownError(
            "%s: TideLink link is DOWN (SWI_LANE_STATUS=0x%08X fcsm=%d cal_done=%d; "
            "need fcsm=%d and cal_done=1).\n"
            "  Refusing a peer-aperture access: it traverses the die-to-die link and "
            "on a down link the transaction never completes, hanging the PS AXI bus "
            "with no timeout (JTAG POR to recover).\n"
            "  Fix: bring the link up on BOTH boards together "
            "(ChipletPair.bringup(deploy=True), or `hetsoc bringup`) — cal_done only "
            "asserts once the peer die is also training over the ribbon."
            % (getattr(board, "name", "board"), status.raw, status.fcsm,
               status.cal_done, 4)
        )


def run_guarded(fn: Callable[..., Any], timeout_s: float, *args: Any,
                **kwargs: Any) -> Any:
    """Call ``fn(*args, **kwargs)`` with a hard wall-clock bound.

    A wedged PS AXI bus makes the *board-side* read block forever; the host-side
    SSH read then blocks forever too. This runs the call on a daemon thread and
    converts "did not return in time" into ``WedgeDetected``, so a hung board
    fails a test instead of hanging the runner.

    The worker thread is a daemon and is deliberately NOT joined on timeout —
    it may be blocked in an unkillable read. The interpreter can still exit.
    """
    box = {}

    def _target() -> None:
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:            # noqa: BLE001 — re-raised below
            box["error"] = exc

    worker = threading.Thread(
        target=_target, daemon=True,
        name="hetsoc-guarded-%s" % getattr(fn, "__name__", "call"))
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise WedgeDetected(
            "%s() did not return within %.1fs — treating the board as WEDGED.\n"
            "  A PS access to an address the SoC does not decode, or a peer access "
            "over a down/stalled link, hangs the ZynqMP AXI bus with no timeout.\n"
            "  Do NOT retry: recover with a JTAG POR "
            "(`hetsoc recover <board>` / fpgahub reset on mapstone-dev), then "
            "re-deploy before bringing the link up again."
            % (getattr(fn, "__name__", "call"), timeout_s)
        )
    if "error" in box:
        raise box["error"]
    return box.get("value")


def guarded(timeout_s: float) -> Callable[[F], F]:
    """Decorator: turn a hang in the wrapped call into ``WedgeDetected``.

    Usage::

        @guarded(5.0)
        def read_status(board):
            return board.read(...)

    Every board-touching entry point in this package is wrapped with this. The
    timeout is recorded on the wrapper as ``__hetsoc_timeout__`` so tests can
    assert that a code path is bounded at all.
    """
    if timeout_s <= 0:
        raise ValueError("guarded() timeout must be > 0, got %r" % (timeout_s,))

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return run_guarded(fn, timeout_s, *args, **kwargs)

        wrapper.__hetsoc_timeout__ = timeout_s      # type: ignore[attr-defined]
        wrapper.__hetsoc_unguarded__ = fn           # type: ignore[attr-defined]
        return wrapper                              # type: ignore[return-value]

    return decorator
