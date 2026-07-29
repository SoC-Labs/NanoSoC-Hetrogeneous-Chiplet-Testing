# =============================================================================
# hetsoc.board — one KR260, and the single choke point for board access.
#
# EVERY access goes: soc_addr -> Target.to_host() -> Transport -> agent.
#         guard #1 --^                    guard #2 --^  (window re-check)
#
# There is no other path. `Board` deliberately exposes no "raw" or "unsafe"
# accessor and never hands out the transport's physical address space, because
# the failure it is preventing is not recoverable in software: a PS access to a
# PL address the SoC does not decode hangs the ZynqMP AXI bus with no timeout,
# the board goes to 100% packet loss, and only a JTAG POR brings it back.
#
# THREE RULES ENFORCED HERE
#   1. Address bound   — delegated to Target.to_host(); out-of-window raises
#                        before anything is issued.
#   2. Peer gate       — ANY access whose upper address byte is the peer
#                        aperture (0x2F) leaves the die, so it is preceded by
#                        require_link_up(). Not "at the top of a script" like
#                        kr260_eth_xfer.py:_require_link(), but on every single
#                        access, because a link can drop mid-run.
#   3. Time bound      — every operation is wrapped in run_guarded(), so a hung
#                        bus raises WedgeDetected instead of blocking forever.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""The Board object: guarded, timeout-bounded access to one KR260's SoC."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Union

from . import regs
from .log import get_logger
from .regs import LaneStatus
from .safety import (ConfigError, DEFAULT_TIMEOUT_S, HetsocError,
                     TransportError, WedgeDetected, require_link_up, run_guarded)
from .targets import Target, get_target
from .transport import Transport, make_transport

__all__ = ["Board"]

_log = get_logger("board")


class Board:
    """One KR260 running one chiplet bitstream. All access goes through the guard.

    Args:
        host: SSH destination, ``"ubuntu@10.22.24.159"`` (or a bare IP; the
            ``ubuntu`` user is assumed, matching kr260_deploy.sh).
        target: registry name or a :class:`~hetsoc.targets.Target`.
        role: ``"die_a"`` (master/grandmaster) or ``"die_b"`` (slave). die_a is
            pinned grandmaster by ROLE_CFG master-lock; do not rely on
            auto-election (KR260_BENCH_RUNBOOK.md §6, G1).
        name: short label for logs/reports; defaults to the role.
        fpgahub: fpgahub board name (``"kr260_01"``), needed for POR/deploy.
    """

    def __init__(self, host: str, target: Union[str, Target], role: str,
                 name: str = "", fpgahub: Optional[str] = None,
                 transport: Optional[Transport] = None,
                 transport_kind: Optional[str] = None,
                 password: Optional[str] = None,
                 proxy: Optional[str] = None,
                 deploy_command: Optional[str] = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.target: Target = get_target(target)
        if role not in self.target.roles:
            raise ConfigError(
                "board %r: role %r is not one of %s for target %s. die_a is the "
                "master/grandmaster, die_b the slave; the two boards MUST carry "
                "different roles and matching (mirrored) images — the same image "
                "on both drives two outputs onto every ribbon lane."
                % (name or host, role, ", ".join(self.target.roles),
                   self.target.name))
        self.host = host if "@" in str(host) else "ubuntu@%s" % host
        self.role = role
        self.name = name or role
        self.fpgahub_name = fpgahub
        self.deploy_command = deploy_command
        self.timeout_s = timeout_s
        self._transport_kind = transport_kind
        self._transport = transport
        self._password = password
        self._proxy = proxy
        #: True when the die has not been touched since a deploy or a POR — the
        #: only state in which re-running the link bring-up is safe. See
        #: `ChipletPair.bringup`.
        self._fresh = False
        self.log = _log.bind(board=self.name, role=role, target=self.target.name)

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #
    @property
    def transport(self) -> Transport:
        """The (lazily created, lazily connected) transport for this board."""
        if self._transport is None:
            if not self.target.resolved:
                # Refuse to even build a transport: its window would be a guess.
                self.target.to_host(0)          # raises ProvisionalTargetError
            self._transport = make_transport(
                self._transport_kind, self.host,
                self.target.window_base, self.target.window_size,
                password=self._password, proxy=self._proxy,
                timeout_s=self.timeout_s)
        return self._transport

    def open(self) -> "Board":
        self.transport.open()
        return self

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()

    def __enter__(self) -> "Board":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Core access — the guarded choke point
    # ------------------------------------------------------------------ #
    def _prepare(self, soc_addr: int, op: str, count: int = 1) -> int:
        """Translate + gate. Returns the PS physical address."""
        host_addr = self.target.to_host(soc_addr)
        if count > 1:
            # The last word must be in-window too, or a burst walks off the end.
            self.target.to_host(soc_addr + 4 * (count - 1))
        if self.target.is_peer(soc_addr):
            # RULE 2. This access LEAVES THE DIE. On a down link the transaction
            # never completes and hangs the PS bus.
            require_link_up(self)
            self.log.debug("peer access (link verified)", op=op, soc=soc_addr,
                           host=host_addr)
        return host_addr

    def read(self, soc_addr: int) -> int:
        """Read one 32-bit word at a SoC-internal address."""
        host_addr = self._prepare(soc_addr, "read")
        value = self._guarded(self.transport.read32, host_addr)
        self.log.debug("read", soc=soc_addr, host=host_addr, value=value)
        return value

    def write(self, soc_addr: int, value: int) -> None:
        """Write one 32-bit word at a SoC-internal address."""
        host_addr = self._prepare(soc_addr, "write")
        self._guarded(self.transport.write32, host_addr, value & 0xFFFFFFFF)
        self.log.debug("write", soc=soc_addr, host=host_addr, value=value)

    def read_many(self, soc_addr: int, n: int) -> List[int]:
        """Read *n* consecutive 32-bit words starting at *soc_addr*."""
        if n < 0:
            raise ValueError("read_many(n=%d): n must be >= 0" % n)
        if n == 0:
            return []
        host_addr = self._prepare(soc_addr, "read_many", count=n)
        values = self._guarded(self.transport.read_many, host_addr, n)
        self.log.debug("read_many", soc=soc_addr, host=host_addr, n=n)
        return list(values)

    def _guarded(self, fn: Any, *args: Any) -> Any:
        """RULE 3: every board operation is time-bounded."""
        try:
            return run_guarded(fn, self.timeout_s, *args)
        except WedgeDetected:
            self.log.critical(
                "WEDGE: board stopped responding — JTAG POR required",
                host=self.host, fpgahub=self.fpgahub_name or "(unset)")
            raise

    # ------------------------------------------------------------------ #
    # Register conveniences
    # ------------------------------------------------------------------ #
    def reg(self, offset: int) -> int:
        """SoC address of a TideLink APB register from its shared offset.

        ``board.reg(regs.SWI_LANE_STATUS)`` is the unambiguous form: the offsets
        in ``hetsoc.regs`` are TLAPB-relative, and the TLAPB base is per-target.
        """
        return self.target.reg(offset)

    def reg_read(self, offset: int) -> int:
        """Read a TideLink APB register by its TLAPB-relative offset."""
        return self.read(self.target.reg(offset))

    def reg_write(self, offset: int, value: int) -> None:
        """Write a TideLink APB register by its TLAPB-relative offset."""
        self.write(self.target.reg(offset), value)

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #
    def lane_status(self) -> LaneStatus:
        """Read + decode ``SWI_LANE_STATUS`` (RO; safe with the link down).

        This window is readable with the link DOWN — the ``tx_open`` gate in
        chiplet_d2d_decode applies only to the TX aperture, and the APB registers
        reset on ``hresetn``, not on role-lock (docs/STATUS_REGISTERS.md §1).
        That is the whole point: these bits are needed precisely when the link is
        not up.
        """
        return regs.decode_lane_status(self.reg_read(regs.SWI_LANE_STATUS))

    def link_up(self) -> bool:
        """True iff this die reports FCSM=4 (LINK_IDLE) and cal_done=1.

        Read-only and wedge-safe. NOTE it is a *local* view: the link is only
        truly up when BOTH dies report this (``ChipletPair.verify_link``).
        """
        return self.lane_status().link_up

    def role_status(self) -> Dict[str, int]:
        """Decode ``ROLE_STATUS`` (0x2E032084).

        ``effective_role`` is INVERTED with respect to "is master": 0 == master
        (docs/STATUS_REGISTERS.md §6.1). die_a must read 0, die_b must read 1 —
        that mismatch is how a swapped die_a/die_b image pair is caught.
        """
        raw = self.reg_read(regs.ROLE_STATUS)
        return {
            "raw": raw,
            "effective_role": raw & 1,
            "is_master": int((raw & 1) == 0),
            "role_locked": (raw >> 1) & 1,
            "expected_role": 0 if self.role == "die_a" else 1,
            "role_ok": int((raw & 1) == (0 if self.role == "die_a" else 1)),
        }

    def alive(self) -> bool:
        """Boot-ROM aliveness probe. Reads only; cannot wedge.

        Reads the SoC boot ROM's vector table, which is combinational with HREADY
        hardwired high on the free-running system clock — it answers even with
        both Cortex-M0+ cores halted or held in reset (eth_ss_probe.py). A PASS
        proves the PS->SoC backdoor delivers into a live SoC.

        Returns False if the board does not answer (transport failure or wedge).
        Raises ``AddressGuardError``/``ConfigError`` instead of returning False
        when the *descriptor* is at fault — a mis-configured target is a bug in
        the test, not a dead board, and silently reporting "not alive" would hide
        it.
        """
        base = self.target.bootrom_soc_base
        if base is None:
            raise ConfigError(
                "target %s declares no boot-ROM probe address, so there is no "
                "verified wedge-safe aliveness read for it. %s"
                % (self.target.name,
                   self.target.provisional_reason
                   or "Add `bootrom_soc_base`/`bootrom_expect` to the descriptor "
                      "once they are read off real silicon."))
        expect = tuple(self.target.bootrom_expect)
        want = len(expect) or 4
        try:
            words = self.read_many(base, want)
        except (TransportError, WedgeDetected) as exc:
            self.log.error("boot-ROM probe failed", error=str(exc))
            return False

        if not expect:
            plausible = words[0] not in (0x00000000, 0xFFFFFFFF)
            self.log.warning(
                "no golden boot-ROM signature for this target — judging "
                "aliveness on plausibility only (TBD: record the real vector "
                "table off silicon)", word0=words[0], plausible=plausible)
            return plausible

        ok = list(words) == list(expect)
        self.log.info("boot-ROM probe", ok=ok,
                      got=" ".join("0x%08X" % w for w in words))
        if not ok:
            self.log.error(
                "boot-ROM mismatch — the PS->SoC backdoor is not delivering into "
                "the expected SoC. Is the right bitstream loaded "
                "(fpga_manager=operating), and the right die image on this board?",
                expect=" ".join("0x%08X" % w for w in expect))
        return ok

    # ------------------------------------------------------------------ #
    # Freshness — see ChipletPair.bringup
    # ------------------------------------------------------------------ #
    @property
    def is_fresh(self) -> bool:
        """True when the die has been reset/reflashed and never brought up since.

        Re-running the bring-up (``LL_SWRESET``) on an ALREADY-LIVE link desyncs
        it and hangs the sender's peer writes — this wedged die_a on 2026-07-29
        (KR260_BENCH_RUNBOOK.md §10). ``ChipletPair.bringup()`` refuses unless
        both dies are fresh.
        """
        return self._fresh

    def mark_fresh(self) -> None:
        """Record that this die has just been reflashed or POR'd."""
        self._fresh = True

    def mark_live(self) -> None:
        """Record that this die's link has been brought up (no longer fresh)."""
        self._fresh = False

    # ------------------------------------------------------------------ #
    # Board-level operations
    # ------------------------------------------------------------------ #
    def deploy(self) -> None:
        """Reflash this board's bitstream, then mark the die fresh.

        Prefers the fpgahub action declared by the target (one action per board,
        role-aware — KR260_BENCH_RUNBOOK.md §3). A ``deploy_command`` on the
        board (from ``hetsoc.toml``) overrides it; ``{host}``/``{role}``/
        ``{name}``/``{fpgahub}`` are substituted.
        """
        from . import fpgahub as _fpgahub          # lazy: no subprocess at import

        self.close()                               # the PL reload drops the link
        if self.deploy_command:
            command = self.deploy_command.format(
                host=self.host, role=self.role, name=self.name,
                fpgahub=self.fpgahub_name or "", target=self.target.name)
            self.log.info("deploy (command)", command=command)
            _fpgahub.run_local(command, timeout_s=600.0, shell=True)
        elif self.target.deploy_action and self.fpgahub_name:
            self.log.info("deploy (fpgahub action)",
                          action=self.target.deploy_action, board=self.fpgahub_name)
            _fpgahub.run_action(self.fpgahub_name, self.target.deploy_action,
                                timeout_s=600.0)
        else:
            raise ConfigError(
                "board %r has no way to deploy: target %s declares no fpgahub "
                "action%s.\n"
                "  Give the board a `deploy_command` in hetsoc.toml, e.g.\n"
                "      deploy_command = \"make -C <flow> deploy ROLE={role} "
                "KR260_HOST={host}\"\n"
                "  or set `fpgahub = \"kr260_01\"` so the target's action can run."
                % (self.name, self.target.name,
                   "" if self.target.deploy_action
                   else " and no `fpgahub` board name is configured"))
        self.mark_fresh()
        self.log.info("deploy complete — die is FRESH (bring-up is safe)")

    def por(self) -> None:
        """JTAG power-on reset via fpgahub. The only wedge recovery there is.

        A POR also clears ``role_lock`` (POR-only clear,
        docs/STATUS_REGISTERS.md §4), so the die comes back FRESH and the link
        bring-up may legitimately be re-run afterwards.
        """
        from . import fpgahub as _fpgahub          # lazy

        if not self.fpgahub_name:
            raise ConfigError(
                "board %r has no fpgahub name, so it cannot be POR'd. Set "
                "`fpgahub = \"kr260_01\"` in hetsoc.toml — a JTAG POR is the ONLY "
                "recovery from a wedged PS AXI bus." % self.name)
        self.close()
        self.log.warning("issuing JTAG POR", board=self.fpgahub_name)
        _fpgahub.reset(self.fpgahub_name)
        self.mark_fresh()

    def wait_reachable(self, timeout_s: float = 90.0, interval_s: float = 5.0) -> bool:
        """Poll until the board answers again (post-POR it returns in ~10 s)."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                if self.transport.open().ping():
                    self.log.info("board reachable again")
                    return True
            except HetsocError as exc:
                self.log.debug("not yet reachable", error=str(exc))
            self.close()
            time.sleep(interval_s)
        self.log.error("board did not come back", timeout_s=timeout_s)
        return False

    # ------------------------------------------------------------------ #
    def health(self) -> Dict[str, Any]:
        """Link + per-node FC health for this board (see ``hetsoc.health``)."""
        from . import health as _health             # lazy

        return _health.link_health(self)

    def describe(self) -> str:
        return "%s (%s) %s target=%s" % (self.name, self.role, self.host,
                                         self.target.name)

    def __repr__(self) -> str:                                  # pragma: no cover
        return "Board(name=%r, role=%r, host=%r, target=%r)" % (
            self.name, self.role, self.host, self.target.name)
