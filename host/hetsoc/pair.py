# =============================================================================
# hetsoc.pair — the two-board heterogeneous pair.
#
# THE ONE ORDERING RULE THAT COSTS A BENCH SESSION
# ------------------------------------------------
# Re-running the link bring-up (`LL_SWRESET`) on an ALREADY-LIVE link desyncs it
# and hangs the sender's peer writes. That wedged die_a on 2026-07-29
# (KR260_BENCH_RUNBOOK.md §10). So `bringup()` REFUSES unless both dies are
# fresh — straight out of a deploy or a JTAG POR, the only states in which the
# bring-up recipe is safe — or the caller passes `force=True` having checked.
# `verify_link()` is the non-destructive alternative and is what a session with
# an already-live link should call.
#
# WHY BRING-UP IS CONCURRENT
# --------------------------
# `cal_done` only asserts once BOTH dies have role-locked and the forwarded-clock
# RX has trained against the peer over the ribbon. Two sequential bring-ups can
# never converge; the two runs must overlap so they self-synchronise. That is
# why this is a *pair* object and not two board calls.
#
# WHY THE CAM RULE COMES FROM THE DESCRIPTORS
# -------------------------------------------
# The CAM replaces addr[31:24]: match = the SENDER's peer aperture, replace =
# the RECEIVER's address byte for the destination region. On the proven
# homogeneous eth<->eth pair those come from one map. On the HETEROGENEOUS
# eth<->compute pair they do not: ipc_mailbox_0 is 0x23 on the eth die and 0x2A
# on the compute die (nanosoc_compute_soc.yaml:989). A hard-coded 0x00232F01
# aimed at the compute die targets a region it does not decode -> DECERR -> an
# unreturned response -> the exact stall that wedges the PS bus. So the rule is
# always built from {sender.target, receiver.target}.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""The ChipletPair: concurrent bring-up, link verification and cross-die access."""

from __future__ import annotations

import concurrent.futures as futures
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import regs
from .board import Board
from .log import get_logger
from .safety import (AddressGuardError, ConfigError, HetsocError, LinkDownError,
                     require_link_up)

__all__ = ["ChipletPair"]

_log = get_logger("pair")

#: Bring-up poll budgets. cal_done needs the peer die + ribbon, so it is the
#: long one; a timeout there almost always means "peer not up / ribbon", not
#: "this die is broken" (kr260_eth_bringup.py:191-194).
CAL_TIMEOUT_S = 20.0
CONVERGE_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.02
_LL_SETTLE_S = 0.005


class ChipletPair:
    """Two boards joined by the J21 ribbon, driven as one die-to-die pair."""

    def __init__(self, a: Board, b: Board) -> None:
        if a is b:
            raise ConfigError("a ChipletPair needs two distinct boards")
        if a.role == b.role:
            raise ConfigError(
                "both boards claim role %r. The pair must be die_a (master/"
                "grandmaster) + die_b (slave), with MIRRORED images — the same "
                "image on both drives two outputs onto every ribbon lane "
                "(KR260_BENCH_RUNBOOK.md §1)." % a.role)
        if a.host == b.host:
            raise ConfigError("both boards point at the same host %r" % a.host)
        self.a = a
        self.b = b
        self.log = _log.bind(a=a.name, b=b.name)

    # ------------------------------------------------------------------ #
    @property
    def boards(self) -> Tuple[Board, Board]:
        return (self.a, self.b)

    @property
    def heterogeneous(self) -> bool:
        """True when the two dies run different designs (the point of this repo)."""
        return self.a.target.name != self.b.target.name

    def other(self, board: Board) -> Board:
        """The far die — the one a cross-die access from *board* lands on."""
        if board is self.a:
            return self.b
        if board is self.b:
            return self.a
        raise ConfigError("board %r is not a member of this pair"
                          % getattr(board, "name", board))

    def close(self) -> None:
        for board in self.boards:
            board.close()

    def __enter__(self) -> "ChipletPair":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Bring-up
    # ------------------------------------------------------------------ #
    def bringup(self, deploy: bool = False, force: bool = False,
                cal_timeout_s: float = CAL_TIMEOUT_S,
                converge_timeout_s: float = CONVERGE_TIMEOUT_S) -> None:
        """Bring the die-to-die link up on BOTH dies concurrently.

        Args:
            deploy: reflash both boards first. This is the SAFE design-iteration
                flow: the link is then only ever brought up on FRESH dies.
            force: bring up even though the dies are not known-fresh. Only pass
                this having confirmed, read-only, that the link is DOWN on both
                dies — see the refusal message.

        Raises:
            RuntimeError: the dies are not fresh and *force* is not set.
            LinkDownError: the link did not converge to FCSM=4 on both dies.
        """
        if deploy:
            self.log.info("deploying both dies (concurrent)")
            self._both(lambda: self.a.deploy(), lambda: self.b.deploy())

        if not force and not (self.a.is_fresh and self.b.is_fresh):
            self._refuse_bringup()

        self.log.info("bringing the link up on BOTH dies concurrently",
                      cal_timeout_s=cal_timeout_s,
                      converge_timeout_s=converge_timeout_s)
        results = self._both(
            lambda: self._bringup_one(self.a, cal_timeout_s, converge_timeout_s),
            lambda: self._bringup_one(self.b, cal_timeout_s, converge_timeout_s))

        for board in self.boards:
            board.mark_live()

        up_a, up_b = results[0].link_up, results[1].link_up
        if not (up_a and up_b):
            raise LinkDownError(
                "link did NOT converge bilaterally: %s=%s (fcsm=%d cal=%d), "
                "%s=%s (fcsm=%d cal=%d).\n"
                "  cal_done gates on the PEER die training over the ribbon, so a "
                "cal_done=0 usually means: peer not brought up at the same time, "
                "ribbon not seated, or the die_a/die_b images are swapped (same "
                "image on both drives two outputs onto every lane).\n"
                "  Judge by FCSM, not lane-lock: lane_locked self-deasserts to "
                "0x00 after training and that is expected."
                % (self.a.name, "UP" if up_a else "DOWN", results[0].fcsm,
                   results[0].cal_done,
                   self.b.name, "UP" if up_b else "DOWN", results[1].fcsm,
                   results[1].cal_done))
        self.log.info("LINK UP — FCSM=4 (LINK_IDLE), cal_done=1 on both dies")

    def _refuse_bringup(self) -> None:
        """Fail loud rather than desync a live link."""
        live = []
        unknown = []
        for board in self.boards:
            try:
                if board.link_up():
                    live.append(board.name)
            except HetsocError as exc:
                unknown.append("%s (%s)" % (board.name, exc.__class__.__name__))
        detail = ""
        if live:
            detail = ("\n  The link is CURRENTLY UP on: %s. Re-running the "
                      "bring-up on a live link is the specific action that "
                      "desyncs it and hangs the sender's peer writes. Use "
                      "verify_link() instead." % ", ".join(live))
        elif unknown:
            detail = ("\n  Could not read the link state on: %s — do not force "
                      "blind." % ", ".join(unknown))
        raise RuntimeError(
            "refusing to bring up the link: the dies are not FRESH (%s=%s, %s=%s)."
            "%s\n"
            "  Re-running the bring-up (LL_SWRESET) on an already-live link "
            "desyncs it and hangs the sender — this wedged die_a on 2026-07-29 "
            "(KR260_BENCH_RUNBOOK.md §10).\n"
            "  Do one of:\n"
            "    * bringup(deploy=True)  — reflash both dies first (the design-"
            "iteration flow; the link is then only brought up on fresh dies);\n"
            "    * board.por()           — JTAG POR both boards (clears role_lock, "
            "which is POR-only), then bring up;\n"
            "    * verify_link()         — if the link is already up, just check it;\n"
            "    * bringup(force=True)   — ONLY after confirming read-only that "
            "the link is DOWN on both dies."
            % (self.a.name, "fresh" if self.a.is_fresh else "not fresh",
               self.b.name, "fresh" if self.b.is_fresh else "not fresh", detail))

    def _bringup_one(self, board: Board, cal_timeout_s: float,
                     converge_timeout_s: float) -> regs.LaneStatus:
        """The per-die bring-up recipe (kr260_eth_bringup.py:bringup)."""
        log = board.log
        want_role = 0 if board.role == "die_a" else 1
        cfg = regs.ROLE_CFG_MASTER_LOCK if want_role == 0 else regs.ROLE_CFG_SLAVE_LOCK

        # 1. role select + lock. ROLE_CFG is at 0x2080 (NOT 0x2084 — writing
        #    0x2084 lands on the next register and the role silently never locks).
        log.info("bringup 1/4: ROLE_CFG <- 0x%02X (role_lock=1, role=%d)"
                 % (cfg, want_role))
        board.reg_write(regs.ROLE_CFG, cfg)
        time.sleep(0.01)
        effective = board.reg_read(regs.ROLE_STATUS) & 1
        if effective != want_role:
            log.warning("effective_role does not match yet", got=effective,
                        want=want_role)

        # 2. calibration. Gated on the PEER die + ribbon, hence the long budget.
        log.info("bringup 2/4: poll calibration_done (needs the peer die + ribbon)")
        status = self._poll(board, lambda st: st.cal_done == 1, cal_timeout_s)
        if not status.cal_done:
            log.error("cal_done did not assert",
                      timeout_s=cal_timeout_s, raw=status.raw)
            return status

        # 3. to data mode: drop training, then the 3-write LL bootstrap. Order
        #    matters — swreset first clears the CR/CRACK stickies.
        log.info("bringup 3/4: SWI_TRAINING_MODE<-0, then the LL enable sequence")
        board.reg_write(regs.SWI_TRAINING_MODE, 0)
        time.sleep(_LL_SETTLE_S)
        for value in (regs.LL_SWRESET_ON, regs.LL_SWRESET_OFF, regs.LL_ENABLE):
            board.reg_write(regs.WL_LINK_ENABLE_RESET, value)
            time.sleep(_LL_SETTLE_S)

        # 4. converge to FCSM=4 (LINK_IDLE).
        log.info("bringup 4/4: poll FCSM==%d (LINK_IDLE)" % regs.FCSM_LINK_IDLE)
        status = self._poll(board, lambda st: st.link_up, converge_timeout_s)
        log.info("bringup done", fcsm=status.fcsm, cal_done=status.cal_done,
                 up=status.link_up, raw=status.raw)
        return status

    @staticmethod
    def _poll(board: Board, predicate: Callable[[regs.LaneStatus], bool],
              timeout_s: float) -> regs.LaneStatus:
        deadline = time.time() + timeout_s
        status = board.lane_status()
        while time.time() < deadline:
            status = board.lane_status()
            if predicate(status):
                return status
            time.sleep(_POLL_INTERVAL_S)
        return status

    @staticmethod
    def _both(fn_a: Callable[[], Any], fn_b: Callable[[], Any]) -> List[Any]:
        """Run both dies' work concurrently (kr260_eth_regress.both_concurrent)."""
        with futures.ThreadPoolExecutor(max_workers=2) as pool:
            future_a, future_b = pool.submit(fn_a), pool.submit(fn_b)
            return [future_a.result(), future_b.result()]

    # ------------------------------------------------------------------ #
    # Verification (read-only — always safe)
    # ------------------------------------------------------------------ #
    def verify_link(self) -> bool:
        """Read-only: True iff BOTH dies report FCSM=4 and cal_done=1.

        The non-destructive alternative to ``bringup()``. Use it whenever the
        link may already be live: it reads RO APB registers only and cannot
        desync anything.
        """
        status_a = self.a.lane_status()
        status_b = self.b.lane_status()
        up = status_a.link_up and status_b.link_up
        self.log.info("verify_link", up=up,
                      a_fcsm=status_a.fcsm, a_cal=status_a.cal_done,
                      b_fcsm=status_b.fcsm, b_cal=status_b.cal_done)
        return up

    def require_link(self) -> None:
        """Raise ``LinkDownError`` unless both dies report the link up."""
        for board in self.boards:
            require_link_up(board)

    def alive(self) -> Dict[str, bool]:
        """Boot-ROM aliveness on both dies (read-only, wedge-safe)."""
        return {board.name: board.alive() for board in self.boards}

    def roles_ok(self) -> bool:
        """True iff die_a reads master(0) and die_b reads slave(1).

        A mismatch means the die_a/die_b images are swapped — which also means
        the ribbon has two drivers per lane. Check this before anything else.
        """
        results = {board.name: board.role_status() for board in self.boards}
        ok = all(r["role_ok"] for r in results.values())
        self.log.info("role straps", ok=ok,
                      **{k: v["effective_role"] for k, v in results.items()})
        return ok

    # ------------------------------------------------------------------ #
    # Address translator (CAM)
    # ------------------------------------------------------------------ #
    def program_cam(self, board: Board, match: int, replace: int,
                    enable: bool = True, allow_unmapped: bool = False) -> None:
        """Program *board*'s outbound address-translator CAM rule 0.

        Three writes, ``CTRL`` armed LAST so a half-configured rule is never
        live (docs/PEER_APERTURE_PROGRAMMING.md §4).

        *match* must be this die's peer aperture and *replace* must be a region
        the FAR die actually decodes. Both are checked: the far die's inbound
        initiator reaches exactly two targets and everything else DECERRs, and an
        unreturned response is how the cross-die path wedges the PS bus. Pass
        ``allow_unmapped=True`` only for a deliberate confinement/negative test
        (CROSS_DIE_TEST_BACKLOG.md item 6) — with a JTAG POR staged.

        The translator resets on ``hresetn``, unlike ROLE_CFG (which is POR-only),
        so it must be reprogrammed after every warm reset.
        """
        far = self.other(board)
        if match != board.target.peer_aperture:
            raise AddressGuardError(
                "program_cam(%s): match byte 0x%02X is not this die's peer "
                "aperture (0x%02X). The CAM only ever fires for addresses inside "
                "the peer window, so a different match byte is a dead rule."
                % (board.name, match, board.target.peer_aperture))
        legal = sorted(far.target.inbound_targets.items())
        if replace not in [byte for _n, byte in legal] and not allow_unmapped:
            raise AddressGuardError(
                "program_cam(%s -> %s): replace byte 0x%02X is not an inbound "
                "target of the FAR die (%s).\n"
                "  Inbound from the far die reaches EXACTLY: %s.\n"
                "  Anything else takes a DECERR from the far die's top-level "
                "default slave, and an unreturned response is how the cross-die "
                "path wedges the PS bus (docs/CROSS_DIE_WEDGE_ROOTCAUSE.md).\n"
                "  NOTE the pair is %s: take the replace byte from the RECEIVING "
                "die's descriptor, not the sender's.\n"
                "  Deliberate negative test? pass allow_unmapped=True, with a "
                "JTAG POR staged."
                % (board.name, far.name, replace, far.target.name,
                   ", ".join("%s@0x%02X" % (n, v) for n, v in legal) or "(none)",
                   "HETEROGENEOUS" if self.heterogeneous else "homogeneous"))

        rule = regs.cam_rule(match, replace, enable)
        board.log.info("CAM rule 0", rule=rule, match=match, replace=replace,
                       enable=enable, far=far.name)
        board.reg_write(regs.CAM_BASE, 0x00000000)       # BASE_OFFSET = 0
        board.reg_write(regs.CAM_RULE_0, rule)
        board.reg_write(regs.CAM_CTRL, 1 if enable else 0)   # arm LAST

        readback = board.reg_read(regs.CAM_RULE_0)
        if readback != rule:
            raise HetsocError(
                "CAM RULE_0 readback mismatch on %s: wrote 0x%08X, read 0x%08X. "
                "The translator resets on hresetn — did something reset the die "
                "between the write and the read?"
                % (board.name, rule, readback))

    def map_peer_to(self, board: Board, which: str, enable: bool = True) -> int:
        """Program *board*'s CAM to land peer traffic in the far die's *which*.

        The heterogeneity-safe form of ``program_cam``: both bytes come from the
        descriptors, so ``map_peer_to(eth, "ipc_mailbox")`` emits 0x00232F01
        against another eth die and 0x002A2F01 against a compute die, with no
        caller-visible difference.
        """
        far = self.other(board)
        match = board.target.peer_aperture
        replace = far.target.inbound_byte(which)
        self.program_cam(board, match, replace, enable)
        return regs.cam_rule(match, replace, enable)

    # ------------------------------------------------------------------ #
    # Cross-die data plane
    # ------------------------------------------------------------------ #
    def peer_write(self, src: Board, soc_addr: int, value: int) -> None:
        """Write *value* into *src*'s peer aperture — the access that crosses.

        Fire-and-forget: the write returns via HREADY once the link accepts it.
        The CAM must already be programmed (``map_peer_to``) or the far die
        DECERRs. ``require_link_up`` runs first, unconditionally.
        """
        if not src.target.is_peer(soc_addr):
            raise AddressGuardError(
                "peer_write(%s, 0x%08X): that address is not in this die's peer "
                "aperture (0x%02X000000..0x%02XFFFFFF), so it would NOT cross the "
                "link — it would hit this die's own memory map. Use "
                "src.target.peer(offset) to build the address."
                % (src.name, soc_addr, src.target.peer_aperture,
                   src.target.peer_aperture))
        require_link_up(src)
        src.log.info("peer write (crosses the link)", soc=soc_addr, value=value,
                     far=self.other(src).name)
        src.write(soc_addr, value)

    def peer_read(self, src: Board, soc_addr: int) -> int:
        """Read back over the link. WEDGE-PRONE — prefer a far-die local read.

        The peer read round-trip intermittently hangs on current silicon and
        wedged both boards on 2026-07-29. Data integrity is covered wedge-safely
        by having the RECEIVING die read its own memory (``read_landed``); this
        exists to exercise the read return path in an attended session only.
        """
        if not src.target.is_peer(soc_addr):
            raise AddressGuardError(
                "peer_read(%s, 0x%08X): not in this die's peer aperture."
                % (src.name, soc_addr))
        require_link_up(src)
        src.log.warning("peer READ over the link — WEDGE-PRONE on current silicon",
                        soc=soc_addr)
        return src.read(soc_addr)

    def read_landed(self, dst: Board, which: str, offset: int = 0) -> int:
        """Read where a cross-die write should have landed, on the FAR die.

        A LOCAL read on the receiving die — no link traversal, always safe. This
        is the primary verdict for every cross-die transfer test, exactly as
        ``kr260_eth_xfer.py --mode recv`` is.
        """
        return dst.read(dst.target.inbound_soc_base(which) + offset)

    def cross_die_write(self, src: Board, which: str, offset: int,
                        value: int) -> int:
        """Program the CAM, then peer-write — the whole proven transfer, once.

        Returns the far-die SoC address the payload should land at, so a test
        can verify with ``read_landed``.
        """
        self.map_peer_to(src, which)
        peer_addr = src.target.peer(offset)
        self.peer_write(src, peer_addr, value)
        return self.other(src).target.inbound_soc_base(which) + offset

    # ------------------------------------------------------------------ #
    # IPC mailbox — the second inbound target
    # ------------------------------------------------------------------ #
    def mailbox_send(self, src: Board, words: Sequence[int]) -> None:
        """Write a slot-0 message across the link, then raise MSG_VALID.

        Data words first, ``SLOT0_CTRL = MSG_VALID`` last, so the receiver never
        observes a valid message with stale data.
        """
        if len(words) > regs.IPC_SLOT0_NWORDS:
            raise ValueError("slot 0 carries %d words, got %d"
                             % (regs.IPC_SLOT0_NWORDS, len(words)))
        self.map_peer_to(src, "ipc_mailbox")
        for index, word in enumerate(words):
            self.peer_write(src, src.target.peer(regs.IPC_SLOT0_DATA + 4 * index),
                            word)
        self.peer_write(src, src.target.peer(regs.IPC_SLOT0_CTRL), regs.IPC_MSG_VALID)
        src.log.info("mailbox message issued", words=len(words),
                     far=self.other(src).name)

    def mailbox_recv(self, dst: Board) -> Dict[str, Any]:
        """Read the local mailbox on the RECEIVING die (local, always safe).

        ``irq_latched`` is the cross-die INTERRUPT SOURCE: a far-die write
        latched the near-die mailbox IRQ, which feeds the CPU1 NVIC. Delivery to
        an ISR needs firmware and is out of scope here (backlog item 8) — this
        proves the source fires.
        """
        base = dst.target.inbound_soc_base("ipc_mailbox")
        words = dst.read_many(base + regs.IPC_SLOT0_DATA, regs.IPC_SLOT0_NWORDS)
        ctrl = dst.read(base + regs.IPC_SLOT0_CTRL)
        irq = dst.read(base + regs.IPC_IRQ_STATUS)
        return {
            "base": base,
            "words": words,
            "ctrl": ctrl,
            "msg_valid": int(bool(ctrl & regs.IPC_MSG_VALID)),
            "ack": int(bool(ctrl & regs.IPC_ACK)),
            "irq_status": irq,
            "irq_latched": irq & 1,
        }

    # ------------------------------------------------------------------ #
    # Health / soak
    # ------------------------------------------------------------------ #
    def health(self, board: Board) -> Dict[str, Any]:
        """Full read-only health sample for one die of the pair."""
        from . import health as _health           # lazy

        return _health.link_health(board)

    def health_both(self) -> Dict[str, Dict[str, Any]]:
        """Health for both dies. The diagnostic to take BEFORE and AFTER traffic."""
        return {board.name: self.health(board) for board in self.boards}

    def soak(self, src: Board, iters: int, payload: int = 0xC0FFEE01,
             which: str = "shared_sram", offset: int = 0x1000,
             window_words: int = 16, sample_every: int = 50,
             stop_on_degrade: bool = True) -> Dict[str, Any]:
        """Sustained peer-WRITE load with FC-health sampling between batches.

        WRITE-ONLY by design. A peer write that stalls still returns via HREADY;
        the per-beat peer READ round-trip is the wedge-prone one and is not done
        here (use ``peer_read`` deliberately, attended). Integrity is proved
        wedge-safely by the receiving die's local read.

        ``stop_on_degrade`` implements fix #2 of the wedge root-cause fix path
        (the host interim that needs no rebuild): stop on a rising CRC or a stuck
        Ack/Nack FIFO rather than letting the next transaction wedge the bus.
        """
        from . import health as _health           # lazy

        require_link_up(src)
        self.map_peer_to(src, which)
        base = src.target.peer(offset)

        baseline = _health.link_health(src)
        previous_fc = baseline["fc"]
        fcsm_min, cal_all, sticky_seen = 7, 1, 0
        credit_min, credit_max = regs.CREDIT_COUNT_MASK, 0
        completed = 0
        degraded_at: Optional[int] = None

        for index in range(iters):
            self.peer_write(src, base + (index % window_words) * 4,
                            (payload + index) & 0xFFFFFFFF)
            completed = index + 1
            if index % sample_every == 0:
                status = src.lane_status()
                fcsm_min = min(fcsm_min, status.fcsm)
                cal_all &= status.cal_done
                sticky_seen |= src.reg_read(regs.STATUS) & regs.STATUS_STICKY_MASK
                credit = src.reg_read(regs.CREDIT_COUNT) & regs.CREDIT_COUNT_MASK
                credit_min, credit_max = min(credit_min, credit), max(credit_max, credit)
                sample = _health.sample_between_transfers(src, previous_fc)
                previous_fc = sample["fc"]
                if sample["degrading"]:
                    degraded_at = index
                    src.log.error(
                        "FC health degrading mid-soak — stopping before the next "
                        "transaction wedges the bus", iteration=index)
                    if stop_on_degrade:
                        break

        final = _health.link_health(src)
        ok = (degraded_at is None and fcsm_min == regs.FCSM_LINK_IDLE
              and bool(cal_all) and sticky_seen == 0 and completed == iters)
        result = {
            "iters_requested": iters,
            "iters_completed": completed,
            "fcsm_min": fcsm_min,
            "cal_all": int(cal_all),
            "sticky_seen": sticky_seen,
            "credit_range": (credit_min, credit_max),
            "degraded_at": degraded_at,
            "baseline": baseline,
            "final": final,
            "ok": ok,
        }
        src.log.info("soak complete", ok=ok, completed=completed,
                     fcsm_min=fcsm_min, sticky=sticky_seen)
        return result

    # ------------------------------------------------------------------ #
    def describe(self) -> str:
        return "%s  <-J21->  %s   (%s pair)" % (
            self.a.describe(), self.b.describe(),
            "HETEROGENEOUS" if self.heterogeneous else "homogeneous")

    def __repr__(self) -> str:                                  # pragma: no cover
        return "ChipletPair(a=%r, b=%r)" % (self.a.name, self.b.name)
