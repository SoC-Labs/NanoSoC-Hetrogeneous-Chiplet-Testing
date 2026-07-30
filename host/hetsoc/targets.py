# =============================================================================
# hetsoc.targets — the per-bitstream address-descriptor registry.
#
# WHAT THIS REPLACES
# ------------------
# CHIPLET_HOST_TOOLING_PLAN.md's finding: the proven bench tooling carries
# THREE hard-coded address maps in three code styles, re-hard-coded across six
# scripts. Every one of them is "base is per-target, offset is shared". This
# module is the registry that plan recommends: one auditable, fail-loud table
# per target, carrying not just the base but the target's SAFETY GUARD.
#
# THE GUARD IS THE POINT
# ----------------------
# `to_host()` is the ONLY way an address reaches /dev/mem in this framework.
# On the eth-chiplet, PS phys = 0x4_0000_0000 + soc_addr, and **any PS read of a
# PL address the SoC does not decode hangs the ZynqMP AXI bus with no timeout**
# — the board goes to 100% packet loss and only a JTAG POR recovers it. So:
#
#   * out-of-window            -> AddressGuardError, before anything is issued
#   * in-window but undecoded  -> SAFE: the SoC AHB matrix's CMSDK default slave
#                                 sits on the free-running system clock and
#                                 terminates with HREADY=1 + SLVERR, never a
#                                 hang. This is exactly why the window bound is
#                                 sufficient (KR260_BENCH_RUNBOOK.md §4).
#   * peer aperture (0x2F)     -> additionally gated on FCSM==4, in Board.
#
# STRUCTURAL IMMUNITY TO THE BARE-LINK TRAP
# -----------------------------------------
# The bare-link tooling pokes 0x8000_0000 / 0x8403_xxxx / 0xA400_xxxx. Those are
# UNDECODED on a chiplet bitstream and wedged kr260_01 on first load. A chiplet
# target's window base is *required by construction* to sit above the 32-bit
# boundary (the backdoor is an HPM0_FPD HIGH aperture), so every address
# `to_host()` can emit for a chiplet target is >= 2^32 and therefore CANNOT be a
# bare-link address — it is not a check that can be forgotten, it is arithmetic.
# `_assert_no_bare_link_trap()` re-checks anyway, and a unit test asserts the
# property over the whole registry.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""Target descriptors: SoC address -> PS physical address, with the guard."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from . import regs
from .safety import AddressGuardError, ConfigError, ProvisionalTargetError

__all__ = [
    "Target", "TARGETS", "get_target", "register_target", "target_names",
    "NO_PEER_APERTURE", "BARE_LINK_TRAP_RANGES", "BARE_LINK_CEILING",
    "INBOUND_SHARED_SRAM", "INBOUND_IPC_MAILBOX",
    "load_target_overrides", "target_from_dict",
]

# -----------------------------------------------------------------------------
# Structural constants
# -----------------------------------------------------------------------------
#: A chiplet backdoor window must live above 4 GiB (it is an HPM0_FPD *high*
#: aperture). Enforced in `Target.__post_init__`, which is what makes the
#: bare-link addresses unreachable rather than merely forbidden.
BARE_LINK_CEILING = 1 << 32

#: `peer_aperture` value meaning "this design has no die-to-die peer window".
#: NOT 0 — 0 is a legal address byte, and `is_peer(0x00001000)` must be False.
NO_PEER_APERTURE = -1

#: Canonical inbound-target names. Inbound from the far die reaches EXACTLY
#: these two on the nanoSoC multicore SoC; everything else DECERRs from the
#: top-level default slave (nanosoc_multicore_soc.yaml:2383-2387, quoted in
#: docs/CROSS_DIE_TEST_BACKLOG.md "Load-bearing fact").
INBOUND_SHARED_SRAM = "shared_sram"
INBOUND_IPC_MAILBOX = "ipc_mailbox"

#: Aliases operators/tests reasonably type for the two inbound targets. Anything
#: NOT in here is refused — a test cannot silently aim at a third target.
_INBOUND_ALIASES = {
    "shared_sram": INBOUND_SHARED_SRAM,
    "shared_sram_0": INBOUND_SHARED_SRAM,
    "sram": INBOUND_SHARED_SRAM,
    "ipc_mailbox": INBOUND_IPC_MAILBOX,
    "ipc_mailbox_0": INBOUND_IPC_MAILBOX,
    "mailbox": INBOUND_IPC_MAILBOX,
    "mbox": INBOUND_IPC_MAILBOX,
}

# -----------------------------------------------------------------------------
# The bare-link map — the addresses that WEDGE a chiplet board.
#
# These are the KR260 bases from the bare-link design (tl_socmap.py:76-87). They
# are legitimate for `kr260-bare-link` and lethal for anything else. Listed as
# explicit ranges so the refusal message can name the aperture the caller hit.
# -----------------------------------------------------------------------------
BARE_LINK_TRAP_RANGES: Tuple[Tuple[str, int, int], ...] = (
    ("ahb_sub", 0x8000_0000, 0x0400_0000),
    ("ahb_ptp", 0x8402_0000, 0x0000_1000),
    ("apb", 0x8403_0000, 0x0000_8000),
    ("strap_gpio", 0x8404_0000, 0x0000_1000),
    ("debug_unlock_gpio", 0x8404_1000, 0x0000_1000),
    ("phc_apb", 0x8405_0000, 0x0000_1000),
    ("ahb_tx", 0xA400_0000, 0x0001_0000),
    ("ahb_fifo", 0xA401_0000, 0x0001_0000),
)

#: Bare-link aperture relocation table: (name, canonical Z2 base, KR260 base,
#: size). Verbatim from tl_socmap.py:76-87 — the one good copy of that map.
_BARE_LINK_APERTURES: Tuple[Tuple[str, int, int, int], ...] = (
    ("ahb_sub", 0x4000_0000, 0x8000_0000, 0x0400_0000),
    ("ahb_tx_legacy", 0x4400_0000, 0xA400_0000, 0x0001_0000),
    ("ahb_fifo_legacy", 0x4401_0000, 0xA401_0000, 0x0001_0000),
    ("ahb_ptp", 0x4402_0000, 0x8402_0000, 0x0000_1000),
    ("apb", 0x4403_0000, 0x8403_0000, 0x0000_8000),
    ("strap_gpio", 0x4404_0000, 0x8404_0000, 0x0000_1000),
    ("debug_unlock_gpio", 0x4404_1000, 0x8404_1000, 0x0000_1000),
    ("phc_apb", 0x4405_0000, 0x8405_0000, 0x0000_1000),
    ("ahb_tx", 0x8400_0000, 0xA400_0000, 0x0001_0000),
    ("ahb_fifo", 0x8401_0000, 0xA401_0000, 0x0001_0000),
)


# =============================================================================
# Target
# =============================================================================
@dataclass
class Target:
    """Per-bitstream address descriptor.

    Turns a SoC-internal address into a PS physical address and refuses anything
    unsafe. Two shapes are supported, both affine + bounds-guarded:

    * **window** (chiplet backdoor): ``host = window_base + soc_addr``, valid for
      ``0 <= soc_addr < window_size``. This is the eth-chiplet's ``eth_ss_0``
      HPM0_FPD high aperture.
    * **apertures** (bare-link control design): a table of canonical->host
      aperture relocations; an address outside every aperture is refused, the
      way ``tl_socmap.remap()`` does.

    Attributes:
        name: registry key, e.g. ``"kr260-eth-chiplet"``.
        window_base: PS phys base of the SoC backdoor window.
        window_size: size of that window in bytes (0 == unresolved/provisional).
        inbound_targets: ``{"shared_sram": 0x2D, "ipc_mailbox": 0x23}`` — the
            address bytes the FAR die may land traffic on. Exactly the set the
            far die's inbound initiator decodes; anything else DECERRs.
        peer_aperture: local address byte of the outbound D2D window (0x2F), or
            ``NO_PEER_APERTURE`` for a design with no peer window.
    """

    name: str
    window_base: int
    window_size: int
    inbound_targets: Dict[str, int] = field(default_factory=dict)
    peer_aperture: int = NO_PEER_APERTURE

    # --- extensions (additive; the contract fields above are unchanged) ------
    kind: str = "chiplet"                  # "chiplet" | "bare-link"
    tlapb_base: int = regs.TLAPB_BASE      # SoC HADDR of the TideLink APB
    tidechart_base: int = regs.TIDECHART_BASE
    bootrom_soc_base: Optional[int] = None
    bootrom_expect: Tuple[int, ...] = ()
    apertures: Tuple[Tuple[str, int, int, int], ...] = ()
    #: Extra D2D links on a MULTI-link die. The compute chiplet exposes TWO
    #: TideLinks; the eth and bare-link designs have one and leave this empty.
    #: The scalar fields above (``tlapb_base``, ``tidechart_base``,
    #: ``peer_aperture``) describe LINK 0 — the ribbon default and the only link
    #: carrying a TideChart — and this table is the full per-link map, including
    #: the second link the eth die does not have. Each row is
    #: ``(name, d2d_window_base, d2d_window_size, tlapb_base, tidechart_base,
    #: peer_aperture)`` with ``tidechart_base`` None where the link has no
    #: TideChart. A link's CAM (address-translator) bank is
    #: ``tlapb_base + regs.ADDRXLAT_BANK``; its peer aperture is the
    #: ``haddr[24]==1`` half of the D2D window. SoC-internal only — never a
    #: host address; ``to_host()`` is unchanged.
    d2d_links: Tuple[Tuple[str, int, int, int, Optional[int], int], ...] = ()
    #: Does the PS backdoor actually REACH the D2D window on this die?
    #:
    #: Normally yes — on the eth die the same eth_ss_0 window carries both the
    #: SoC map and the D2D config/peer apertures. On the compute die it is
    #: FALSE: G2 exported `ps_ahb_s`, but `ps_m`'s target list omits d2d0/d2d1
    #: (nanosoc_compute_soc.yaml:1110), so the backdoor reaches the whole
    #: internal map and NONE of the D2D window.
    #:
    #: This must fail loud rather than be left to the board, because the board's
    #: failure mode is SILENT AND MISLEADING: an unreachable read returns
    #: 0x00000000, which decodes as fcsm=0 / cal_done=0 — indistinguishable from
    #: "the link is down". A bring-up script would report a link failure and an
    #: operator would go looking at the ribbon. Measured 2026-07-30, see
    #: docs/SIM_PLAN.md §9c C1.
    ps_reaches_d2d: bool = True
    provisional: bool = False
    provisional_reason: str = ""
    deploy_action: Optional[str] = None    # fpgahub action name, if any
    roles: Tuple[str, ...] = ("die_a", "die_b")
    source: str = ""                       # provenance, cited
    notes: str = ""

    # ------------------------------------------------------------------ #
    def __post_init__(self) -> None:
        if self.kind not in ("chiplet", "bare-link"):
            raise ConfigError("target %r: kind must be 'chiplet' or 'bare-link', "
                              "got %r" % (self.name, self.kind))
        self.inbound_targets = dict(self.inbound_targets)
        for key, byte in self.inbound_targets.items():
            if key not in (INBOUND_SHARED_SRAM, INBOUND_IPC_MAILBOX):
                raise ConfigError(
                    "target %r declares inbound target %r. Inbound from the far "
                    "die reaches EXACTLY two targets on this SoC family: %r and "
                    "%r. Anything else DECERRs on the far side "
                    "(nanosoc_multicore_soc.yaml:2383-2387). If a new design "
                    "really adds a third, widen INBOUND_* here deliberately."
                    % (self.name, key, INBOUND_SHARED_SRAM, INBOUND_IPC_MAILBOX))
            if not 0 <= byte <= 0xFF:
                raise ConfigError("target %r: inbound %r byte 0x%X is not an "
                                  "8-bit address byte" % (self.name, key, byte))
        if self.peer_aperture != NO_PEER_APERTURE and not 0 <= self.peer_aperture <= 0xFF:
            raise ConfigError("target %r: peer_aperture 0x%X is not an 8-bit "
                              "address byte" % (self.name, self.peer_aperture))
        if self.window_size < 0 or self.window_base < 0:
            raise ConfigError("target %r: negative window base/size" % self.name)

        # THE structural invariant. A resolved chiplet window lives above 4 GiB,
        # so no chiplet host address can collide with the 32-bit bare-link map.
        if self.kind == "chiplet" and self.resolved:
            if self.window_base < BARE_LINK_CEILING:
                raise ConfigError(
                    "target %r declares a chiplet backdoor window at PS phys "
                    "0x%X, below the 4 GiB boundary.\n"
                    "  A chiplet backdoor is an HPM0_FPD *high* aperture "
                    "(0x4_0000_0000 on kr260-eth-chiplet, tidelink.hwh:4112). A "
                    "sub-4GiB base would let this descriptor emit bare-link "
                    "addresses (0x8000_0000 / 0x8403_xxxx / 0xA400_xxxx), which "
                    "are UNDECODED on a chiplet bitstream — a read there hangs "
                    "the PS AXI bus with no timeout (JTAG POR to recover).\n"
                    "  Refusing to build the descriptor rather than emit one."
                    % (self.name, self.window_base))
            if self.apertures:
                raise ConfigError("target %r: a chiplet target uses a single "
                                  "backdoor window, not an aperture table"
                                  % self.name)
        if self.provisional and not self.provisional_reason:
            raise ConfigError("target %r is provisional but gives no reason; a "
                              "provisional descriptor must say what is unknown "
                              "and what would resolve it." % self.name)

    # ------------------------------------------------------------------ #
    @property
    def resolved(self) -> bool:
        """True once this descriptor carries a *verified* host address map.

        A provisional target (no bitstream yet, so no known window) has
        ``window_size == 0`` and every host-address request fails loud.
        """
        return self.window_size > 0 or bool(self.apertures)

    @property
    def window_end(self) -> int:
        """First PS phys address *past* the window (exclusive)."""
        return self.window_base + self.window_size

    # ------------------------------------------------------------------ #
    def to_host(self, soc_addr: int) -> int:
        """Translate a SoC-internal address to a PS physical address.

        Raises:
            ProvisionalTargetError: the target has no verified window (no
                bitstream exists yet). Never guessed — a wrong base on ZynqMP is
                an unrecoverable AXI hang.
            AddressGuardError: ``soc_addr`` is outside the window / every
                aperture, is not a non-negative int, or would land on a known
                bare-link trap address.
        """
        if isinstance(soc_addr, bool) or not isinstance(soc_addr, int):
            raise AddressGuardError(
                "%s.to_host(%r): address must be a non-negative int."
                % (self.name, soc_addr))
        if soc_addr < 0:
            raise AddressGuardError(
                "%s.to_host(0x%X): negative address." % (self.name, soc_addr))
        if not self.resolved:
            raise ProvisionalTargetError(self._unresolved_message(soc_addr))

        if self.apertures:
            host = self._relocate(soc_addr)
        else:
            if soc_addr >= self.window_size:
                raise AddressGuardError(
                    "%s.to_host(0x%X): OUTSIDE the SoC backdoor window "
                    "[0x0 .. 0x%X).\n"
                    "  The PS reaches this SoC only through that window (PS phys "
                    "= 0x%X + soc_addr). A PS access to a PL address the SoC does "
                    "not decode hangs the ZynqMP AXI bus with NO TIMEOUT — the "
                    "board drops to 100%% packet loss and only a JTAG POR "
                    "recovers it.\n"
                    "  If you meant a PS *physical* address, do not pass it here: "
                    "to_host() takes SoC-internal addresses (e.g. "
                    "regs.TLAPB_BASE + regs.SWI_LANE_STATUS)."
                    % (self.name, soc_addr, self.window_size, self.window_base))
            host = self.window_base + soc_addr

        self._assert_no_d2d_unreachable(soc_addr)
        self._assert_no_bare_link_trap(host, soc_addr)
        return host

    def _assert_no_d2d_unreachable(self, soc_addr: int) -> None:
        """Refuse D2D-window addresses on a die whose PS port cannot reach them.

        Returning the host address would be worse than useless: the access
        succeeds at the AHB level and reads back zeros, so every TideLink status
        register looks like a down link instead of an unreachable one.
        """
        if self.ps_reaches_d2d:
            return
        for name, wbase, wsize, _tb, _tc, _pa in self.d2d_links:
            if wbase <= soc_addr < wbase + wsize:
                raise AddressGuardError(
                    "%s.to_host(0x%X): inside D2D window %s "
                    "[0x%X .. 0x%X), which this die's PS backdoor CANNOT REACH.\n"
                    "  `ps_m`'s target list omits d2d0/d2d1 "
                    "(nanosoc_compute_soc.yaml:1110), so the backdoor reaches the "
                    "internal map and none of the D2D window: no TideLink APB, no "
                    "CAM, no peer aperture.\n"
                    "  This is refused rather than allowed because the hardware "
                    "failure is SILENT: the read returns 0x00000000, which decodes "
                    "as fcsm=0/cal_done=0 and is indistinguishable from a DOWN "
                    "LINK. You would debug a ribbon that is fine.\n"
                    "  Fix: add d2d0/d2d1 to ps_m's targets in the compute repo, "
                    "then set ps_reaches_d2d=True here. See docs/SIM_PLAN.md 9c C1."
                    % (self.name, soc_addr, name, wbase, wbase + wsize))

    def _relocate(self, addr: int) -> int:
        """Aperture-table relocation (the bare-link shape, per tl_socmap.remap)."""
        for _name, canon_base, host_base, size in self.apertures:
            if canon_base <= addr < canon_base + size:
                return host_base + (addr - canon_base)
        raise AddressGuardError(
            "%s.to_host(0x%08X): not inside any known aperture, so it cannot be "
            "relocated. An unrelocated literal is UNDECODED on this board -> AXI "
            "hang with no timeout (power-cycle to recover).\n"
            "  Known apertures:\n%s"
            % (self.name, addr,
               "\n".join("    %-18s 0x%08X..0x%08X -> 0x%08X"
                         % (n, b, b + s - 1, hb)
                         for n, b, hb, s in self.apertures)))

    def _assert_no_bare_link_trap(self, host: int, soc_addr: int) -> None:
        """Belt-and-braces: never emit a known-undecoded bare-link address.

        Unreachable for a well-formed chiplet target (its window starts above
        4 GiB, see `__post_init__`), which is the intent — this is the assertion
        that the structural property holds, not the mechanism that provides it.
        """
        if self.kind != "chiplet":
            return
        for aperture, base, size in BARE_LINK_TRAP_RANGES:
            if base <= host < base + size:
                raise AddressGuardError(
                    "%s.to_host(0x%X) produced PS phys 0x%08X, which is inside "
                    "the BARE-LINK '%s' aperture (0x%08X..0x%08X).\n"
                    "  Those addresses are UNDECODED on a chiplet bitstream: a "
                    "read there hangs the PS AXI bus with no timeout and wedged "
                    "kr260_01 on first load (KR260_BENCH_RUNBOOK.md §3). Never "
                    "point bare-link tooling (kr260_smoke.py, tl39.py, "
                    "kr260_credit_tx.py) at a chiplet target."
                    % (self.name, soc_addr, host, aperture, base, base + size - 1))
        if host < BARE_LINK_CEILING:
            raise AddressGuardError(
                "%s.to_host(0x%X) produced a 32-bit PS phys 0x%08X. A chiplet "
                "backdoor is a HIGH aperture; anything below 4 GiB on this board "
                "is DDR or undecoded PL." % (self.name, soc_addr, host))

    def _unresolved_message(self, soc_addr: int) -> str:
        return (
            "target %r has NO verified PS address window, so it cannot translate "
            "SoC address 0x%X.\n"
            "  Reason: %s\n"
            "  This is deliberate. A guessed window base on ZynqMP is an "
            "unrecoverable AXI bus hang (JTAG POR only), so this descriptor "
            "refuses to produce a host address rather than invent one.\n"
            "  To resolve: once the bitstream exists, read the window base out of "
            "its .hwh MEMRANGE (as kr260-eth-chiplet's 0x4_0000_0000 was, "
            "tidelink.hwh:4112) and declare it in hetsoc.toml:\n"
            "      [target.%s]\n"
            "      window_base = 0x4_0000_0000   # <- from the real .hwh\n"
            "      window_size = 0x1_0000_0000\n"
            % (self.name, soc_addr, self.provisional_reason or "unresolved",
               self.name))

    # ------------------------------------------------------------------ #
    def is_peer(self, soc_addr: int) -> bool:
        """True if *soc_addr* falls in this die's outbound peer (D2D) aperture.

        A True here means the access LEAVES THE DIE. Every such access must be
        gated on FCSM==4 first (`hetsoc.safety.require_link_up`) — a peer access
        on a down link hangs the PS bus.
        """
        if self.peer_aperture == NO_PEER_APERTURE:
            return False
        if isinstance(soc_addr, bool) or not isinstance(soc_addr, int) or soc_addr < 0:
            return False
        return (soc_addr >> 24) & 0xFF == self.peer_aperture and soc_addr < (1 << 32)

    # ------------------------------------------------------------------ #
    def reg(self, offset: int) -> int:
        """SoC address of a TideLink APB register from its shared offset.

        ``target.reg(regs.SWI_LANE_STATUS)`` — the unambiguous form. Composing
        ``regs.TLAPB_BASE + regs.SWI_LANE_STATUS`` by hand is equivalent for the
        default base but silently wrong for a design that moves the bank.
        """
        return self.tlapb_base + offset

    def tidechart(self, offset: int) -> int:
        """SoC address of a TideChart register from its offset."""
        return self.tidechart_base + offset

    def peer(self, offset: int = 0) -> int:
        """SoC address inside this die's peer aperture, at *offset* into it.

        The CAM replaces ``addr[31:24]`` only and passes ``addr[23:0]`` through,
        so *offset* is also the offset into the far-die region the CAM selects.
        """
        if self.peer_aperture == NO_PEER_APERTURE:
            raise AddressGuardError(
                "target %r has no peer aperture — this design has no outbound "
                "die-to-die window, so there is nothing to address." % self.name)
        if not 0 <= offset < (1 << 24):
            raise AddressGuardError(
                "peer offset 0x%X is outside the 16 MB aperture (0..0xFFFFFF). "
                "The CAM's granularity is one addr[31:24] value." % offset)
        return (self.peer_aperture << 24) | offset

    def inbound_byte(self, which: str) -> int:
        """Address byte of a far-die inbound target, e.g. ``0x2D``.

        Refuses anything that is not one of the exactly-two targets the far
        die's inbound initiator decodes. This is what stops a test aiming at a
        third region: the far die would DECERR, and on current silicon a stalled
        response is how the bus wedges.
        """
        key = _INBOUND_ALIASES.get(str(which).strip().lower())
        if key is None or key not in self.inbound_targets:
            raise AddressGuardError(
                "%r is not an inbound D2D target of %s.\n"
                "  Inbound from the far die reaches EXACTLY these: %s.\n"
                "  Everything else takes a DECERR from the far die's top-level "
                "default slave (nanosoc_multicore_soc.yaml:2383-2387), and an "
                "unreturned response is how the cross-die path wedges the PS bus "
                "(docs/CROSS_DIE_WEDGE_ROOTCAUSE.md)."
                % (which, self.name,
                   ", ".join("%s@0x%02X" % (k, v)
                             for k, v in sorted(self.inbound_targets.items()))
                   or "(none declared)"))
        return self.inbound_targets[key]

    def inbound_soc_base(self, which: str) -> int:
        """Far-die SoC base address of an inbound target, e.g. ``0x2D000000``.

        This is a *far-die local* address — what the receiving board reads to
        verify a transfer landed. It is NOT the address the sender writes.
        """
        return self.inbound_byte(which) << 24

    def cam_rule_for(self, which: str, enable: bool = True,
                     far_target: Optional["Target"] = None) -> int:
        """CAM rule word mapping THIS die's peer aperture onto the FAR die's *which*.

        The match byte is this (sending) die's peer aperture; the replace byte is
        the **receiving** die's address byte for that region.

        >>> HETEROGENEOUS PAIRS <<<
        On a homogeneous pair the two are the same descriptor and *far_target*
        can be omitted — ``cam_rule_for("shared_sram") == 0x002D2F01`` and
        ``cam_rule_for("ipc_mailbox") == 0x00232F01``, the values proven on
        silicon for the eth<->eth pair. On the HETEROGENEOUS eth<->compute pair
        they are NOT the same: the compute die's ``ipc_mailbox_0`` is at 0x2A,
        not 0x23 (nanosoc_compute_soc.yaml:989), so an eth->compute mailbox rule
        is ``0x002A2F01``. Passing the wrong replace byte aims the write at a
        region the far die does not decode; it DECERRs, and on current silicon
        an unreturned response is how the cross-die path wedges the PS bus.
        Always pass *far_target* for a heterogeneous pair —
        ``ChipletPair.program_cam()`` does it for you.
        """
        if self.peer_aperture == NO_PEER_APERTURE:
            raise AddressGuardError(
                "target %r has no peer aperture, so it cannot originate a "
                "cross-die access. %s"
                % (self.name,
                   self.provisional_reason or "No outbound D2D window declared."))
        far = self if far_target is None else far_target
        return regs.cam_rule(self.peer_aperture, far.inbound_byte(which), enable)

    # ------------------------------------------------------------------ #
    def describe(self) -> str:
        """Multi-line human summary — the `hetsoc targets` output."""
        lines = ["%s  (%s)" % (self.name, self.kind)]
        if not self.resolved:
            lines.append("  window      : UNRESOLVED / PROVISIONAL")
            lines.append("  reason      : %s" % self.provisional_reason)
        elif self.apertures:
            lines.append("  apertures   :")
            for n, b, hb, s in self.apertures:
                lines.append("    %-18s 0x%08X..0x%08X -> 0x%08X"
                             % (n, b, b + s - 1, hb))
        else:
            lines.append("  window      : PS phys 0x%X .. 0x%X  (host = base + soc_addr)"
                         % (self.window_base, self.window_end - 1))
        lines.append("  tlapb       : SoC 0x%08X" % self.tlapb_base)
        if self.peer_aperture != NO_PEER_APERTURE:
            lines.append("  peer window : SoC 0x%02X000000 (D2D; requires FCSM=4)"
                         % self.peer_aperture)
        else:
            lines.append("  peer window : none")
        if self.d2d_links:
            lines.append("  d2d links   :")
            for n, wb, ws, tb, tc, pa in self.d2d_links:
                lines.append(
                    "    %-6s D2D 0x%08X (%d MB)  tlapb 0x%08X  peer 0x%02X  %s"
                    % (n, wb, ws >> 20, tb, pa,
                       "tidechart 0x%08X" % tc if tc is not None
                       else "(no tidechart)"))
        if self.inbound_targets:
            lines.append("  inbound     : %s"
                         % ", ".join("%s @0x%02X" % (k, v)
                                     for k, v in sorted(self.inbound_targets.items())))
        if self.source:
            lines.append("  source      : %s" % self.source)
        if self.notes:
            for para in self.notes.strip().splitlines():
                lines.append("  note        : %s" % para.strip())
        return "\n".join(lines)

    def __str__(self) -> str:                                   # pragma: no cover
        return self.name


# =============================================================================
# The built-in registry
# =============================================================================
_ETH_CHIPLET = Target(
    name="kr260-eth-chiplet",
    # HPM0_FPD HIGH aperture. Confirmed in the built bitstream's hardware
    # handoff (tidelink.hwh:4112 MEMRANGE, eth_ss_0) and used by every
    # on-silicon-proven script: kr260_eth_bringup.py:69, kr260_eth_xfer.py:40,
    # eth_ss_probe.py:13. It is NOT 0x8000_0000 — older comments say that and
    # it is out-of-window (it would wedge).
    window_base=0x4_0000_0000,
    window_size=0x1_0000_0000,      # 0x4_0000_0000 .. 0x4_FFFF_FFFF inclusive
    # Exactly two, per nanosoc_multicore_soc.yaml:2383-2387.
    inbound_targets={INBOUND_SHARED_SRAM: 0x2D, INBOUND_IPC_MAILBOX: 0x23},
    # chiplet_d2d_decode.sv:81 — haddr[24]==1 within the 0x2E/0x2F D2D window.
    peer_aperture=0x2F,
    kind="chiplet",
    tlapb_base=0x2E030000,
    tidechart_base=0x2E040000,
    bootrom_soc_base=0x0,
    # Boot-ROM vector table words 0..3: init MSP, reset, NMI, HardFault.
    # Read on real silicon 2026-07-27 (eth_ss_probe.py:15).
    bootrom_expect=(0x18003C00, 0x08000189, 0x080001CD, 0x080001CF),
    deploy_action="deploy_kr260_eth_chiplet_pair",
    source=("tidelink.hwh:4112 (window); kr260_eth_bringup.py:69-83; "
            "kr260_eth_xfer.py:40-69; docs/PEER_APERTURE_PROGRAMMING.md §3-§4; "
            "docs/KR260_BENCH_RUNBOOK.md §4/§6b"),
    notes=("PROVEN ON SILICON 2026-07-27: backdoor alive, FCSM=4 bilateral, "
           "cross-die SRAM + mailbox transfers.\n"
           "die_a image -> one board, die_b (-flip) image -> the other; the same "
           "image on both drives two outputs onto every ribbon lane."),
)

_BARE_LINK = Target(
    name="kr260-bare-link",
    # For an aperture target the window is the hull of the relocated apertures;
    # the aperture table is the real guard (see `_relocate`).
    window_base=0x8000_0000,
    window_size=0x2402_0000,
    inbound_targets={},
    peer_aperture=NO_PEER_APERTURE,
    kind="bare-link",
    apertures=_BARE_LINK_APERTURES,
    tlapb_base=0x4403_0000,        # canonical (Z2) TideLink APB base; relocates
    tidechart_base=0x4406_0000,    # not present on every bare-link target
    source="tl_socmap.py:76-87 (APERTURES); fpga/docs/KR260_PORT.md",
    notes=("The non-chiplet control design: the smaller, route-clean image used "
           "to isolate 'is the ribbon/PHY/bench flow good?' from 'is the chiplet "
           "SoC good?' (KR260_BENCH_RUNBOOK.md §6).\n"
           "Addresses passed to to_host() are CANONICAL (Z2) literals; the "
           "aperture table relocates them. NEVER point this target at a chiplet "
           "board — 0x8403_xxxx is undecoded there and wedges the PS bus."),
)

# -----------------------------------------------------------------------------
# Compute chiplet — PROVISIONAL, and it is the *heterogeneous* half of the pair.
#
# THERE IS NO KR260 PORT. Verified in /home/dam1n19/SoCLabs/NanoSoC-Compute-Chiplet:
#   * no chiplet-level fpga/ directory, no FPGA make target (Makefile targets are
#     bootstrap / chip-boundary / chip-wrapper / elab / clean only);
#   * no *.bit / *.hwh / *.xsa anywhere in the repo or its submodules;
#   * its vendored tidelink ships only mps3 + pynq-z2 targets — no kr260-* target;
#   * the chip boundary (sys_desc/chip_boundary/nanosoc_compute_chiplet.yaml, 81
#     ports) has NO AHB pad group at all. `nanosoc_compute_soc` exposes exactly
#     two external AHB target ports and both are D2D inbound — there is no
#     `*_ss_0` external test slave, i.e. no analogue of eth_ss_0.
# So there is no PS-visible window and no way for a PS to reach this fabric
# except SWD/JTAG. `window_size = 0` makes `to_host()` raise
# ProvisionalTargetError on every call: the descriptor is structurally incapable
# of poking a board, which is the only safe state for a target whose base is
# unknown. KR260 for compute exists only as an unimplemented plan
# (nanosoc-compute-system/docs/KR260_IMPLEMENTATION_PLAN.md:218-229).
#
# The SoC-INTERNAL map below IS carried, because it never reaches /dev/mem on
# its own and because the heterogeneous pair needs it: the far-die inbound
# bytes are what the *eth* die's CAM must be programmed with. As of G4/G5/G7
# that map is now fully populated — per-link TideLink/TideChart bases
# (0x4003/0x6003, 0x4004), the dual D2D windows (0x4000_0000 / 0x6000_0000), and
# the peer apertures (0x41 / 0x61) — so this die can also ORIGINATE cross-die
# CAM rules (compute->eth). The peer values are the post-G4 INTENDED map (the
# decoder implies them; the compute sims still use 0x40 with the decoder
# bypassed — see the TODO(G4) on peer_aperture). None of it enables to_host():
# the window stays unresolved until G2 lands a real bitstream/.hwh.
#
#   >>> THE HETEROGENEITY THAT WILL BITE <<<
#   ipc_mailbox_0 is at 0x2A on the compute die and 0x23 on the eth die.
#   A cross-die mailbox write from eth -> compute therefore needs CAM
#   RULE_0 = 0x002A2F01, NOT the 0x00232F01 that is correct for the proven
#   eth<->eth pair. `ChipletPair.program_cam()` takes the replace byte from the
#   RECEIVING board's descriptor for exactly this reason.
#   (nanosoc_compute_soc.yaml:989 documents why: 0x22-0x23 is the Cortex-M4's
#   SRAM bit-band alias, so the mailbox was kept out of it.)
# -----------------------------------------------------------------------------
_COMPUTE_CHIPLET = Target(
    name="kr260-compute-chiplet",
    # PS BACKDOOR WINDOW — PENDING G2 (docs/BRINGUP_GAPS.md §G2, HARD STOP, in
    # progress). Compute has no PS-backdoor port yet: no eth_ss_0 analogue, no
    # bitstream, no .hwh. This base is the PLANNED MIRROR of the eth backdoor
    # (eth lands at PS phys 0x4_0000_0000 + HADDR, tidelink.hwh:4112) — an
    # HPM0_FPD high aperture the compute port is expected to match. It is
    # recorded, not invented, but window_size STAYS 0 so `resolved` is False and
    # to_host() fails loud on EVERY address. Do NOT set window_size here: the
    # base must be CONFIRMED against the built .hwh once the G2 backdoor lands,
    # and the only sanctioned way to resolve it is a cited hetsoc.toml override
    # (target_from_dict enforces a non-TBD `source`).
    window_base=0x4_0000_0000,      # PLANNED mirror of eth; pending G2 .hwh
    window_size=0,                  # 0 == unresolved -> to_host() fails loud
    # FOUND, cited: nanosoc_compute_soc.yaml:1008 shared_sram_0 @0x2D000000;
    # :989 ipc_mailbox_0 @0x2A000000. d2d0_m / d2d1_m each reach EXACTLY these
    # two (:1091-1100) — the same "exactly two inbound" property the eth die has;
    # __post_init__ confines the descriptor to them (everything else DECERRs on
    # the far side's top-level default slave).
    inbound_targets={INBOUND_SHARED_SRAM: 0x2D, INBOUND_IPC_MAILBOX: 0x2A},
    # PEER APERTURE — LINK 0, post-G4 INTENDED value (docs/BRINGUP_GAPS.md §G4).
    # chiplet_d2d_decode.sv splits the D2D window on haddr[24]; the peer window
    # is the haddr[24]==1 half, so d2d0 @0x4000_0000 -> peer 0x41 (and d2d1
    # @0x6000_0000 -> 0x61, see d2d_links). SoC-internal only: is_peer/peer/
    # cam_rule_for use it to classify addresses and build CAM rule WORDS, none of
    # which reach /dev/mem — and to_host() stays fail-loud until G2 — so an
    # unconfirmed value here cannot wedge a board.
    #   TODO(G4): compute mainline still uses 0x40 (both G2 sims: CAM match=0x40,
    #   RULE_0=0x002D4001) but those testbenches BYPASS chiplet_d2d_decode
    #   (tb_soc_pair.sv:163-164), so they cannot see the haddr[24] split. G4 is
    #   moving the firmware/CAM 0x40 -> 0x41; 0x41 is encoded here as the intended
    #   post-decoder value. Confirm 0x41/0x61 with the decoder IN PATH on a real
    #   build before trusting a cross-die transfer.
    peer_aperture=0x41,
    # C1: G2 landed ps_ahb_s, but ps_m cannot reach d2d0/d2d1. MEASURED
    # 2026-07-30 in sim/het_pair: ps_ahb_s write+read-back to compute
    # shared_sram_0 @0x2D002000 -> 0xa5a5beef (the port works), while
    # 0x40032080/2084/2108 all read 0x00000000 and the role never locks.
    ps_reaches_d2d=False,
    kind="chiplet",
    # LINK 0 config plane (docs/BRINGUP_GAPS.md §G5). 0x2E is mgr_remap_0 (a live
    # peripheral) on this die — NOT a TideLink window; do not reuse eth's 0x2E03.
    tlapb_base=0x40030000,          # link 0 TideLink APB (CAM bank +0x4000)
    tidechart_base=0x40040000,      # link 0 TideChart (link 0 ONLY)
    # Dual-link map. Link 1 mirrors link 0 at +0x2000_0000 and has NO TideChart
    # (tied off, nanosoc_compute_chiplet.sv:656-658). Which link the J21 ribbon
    # lands on is a bench decision; link 0 is the default (it carries TideChart).
    d2d_links=(
        # name,   d2d window,  size(256MB), tlapb,       tidechart,   peer
        ("link0", 0x4000_0000, 0x1000_0000, 0x4003_0000, 0x4004_0000, 0x41),
        ("link1", 0x6000_0000, 0x1000_0000, 0x6003_0000, None,        0x61),
    ),
    bootrom_soc_base=None,          # no verified boot-ROM signature (no bitstream)
    bootrom_expect=(),
    roles=("die_a", "die_b"),
    provisional=True,               # window unresolved until G2 -> still provisional
    provisional_reason=(
        "the NanoSoC Compute Chiplet has NO FPGA/KR260 port yet (G1/G2, HARD "
        "STOP): no bitstream, no .hwh, and no external AHB pad group on the chip "
        "boundary, so the PS-backdoor window base cannot be CONFIRMED. window_base "
        "here is the PLANNED mirror of eth's 0x4_0000_0000 aperture; window_size "
        "stays 0 so to_host() refuses every address until the built .hwh confirms "
        "the base (resolve via a cited hetsoc.toml override). The SoC-internal map "
        "(inbound bytes, per-link TideLink/TideChart bases, the 0x41/0x61 peer "
        "apertures) IS carried — it never reaches /dev/mem — but 0x41/0x61 is the "
        "post-G4 INTENDED value, not yet verified with the decoder in path. It "
        "also has TWO D2D links, so a bench must say WHICH link the ribbon uses"),
    source=("NanoSoC-Compute-Chiplet: nanosoc-compute-system/sys_desc/"
            "nanosoc_compute_soc.yaml:989,1008,1017,1018,1091-1100 (inbound + D2D "
            "windows); src/rtl/nanosoc_compute_chiplet.sv:498,525,665 (2x "
            "chiplet_d2d_decode, 2x tidelink_top), :656-658 (link-1 TideChart tied "
            "off); src/rtl/chiplet_d2d_decode.sv:41-49,113-114,138 (haddr[24] "
            "split -> peer 0x41/0x61). docs/BRINGUP_GAPS.md §G4 (peer aperture), "
            "§G5 (APB base), §G7 (mailbox 0x2A). "
            "WINDOW BASE: pending G2 — planned mirror of eth 0x4_0000_0000, TBD "
            "until confirmed against the built .hwh"),
    notes=("PENDING BITSTREAM (G2): window_base is the planned eth mirror, "
           "window_size=0, so to_host() fails loud; boot-ROM signature unknown.\n"
           "PER-DIRECTION CAM (G7) — the CAM rewrites addr[31:24] on the SENDER "
           "to the RECEIVER's byte, so rules are directional. shared_sram agrees "
           "(0x2D) both ways; the mailbox does NOT (0x2A compute vs 0x23 eth, "
           "because 0x22-0x23 is compute's Cortex-M4 SRAM bit-band alias, "
           "nanosoc_compute_soc.yaml:989):\n"
           "    eth->compute : sram cam_rule(0x2F,0x2D)=0x002D2F01 ; "
           "mailbox cam_rule(0x2F,0x2A)=0x002A2F01\n"
           "    compute->eth : sram cam_rule(0x41,0x2D)=0x002D4101 ; "
           "mailbox cam_rule(0x41,0x23)=0x00234101\n"
           "  Build every rule with cam_rule_for(which, far_target=<receiver>); "
           "it takes the replace byte from the RECEIVER. Applying the eth habit "
           "(replace 0x23) to a COMPUTE receiver is INVALID: 0x23 is not in "
           "compute's inbound set {0x2D,0x2A} -> DECERR, and inbound_byte refuses "
           "it structurally (no compute region maps to 0x23).\n"
           "DUAL LINK: link 0 @0x4000_0000 carries the TideChart and is the "
           "ribbon default; link 1 @0x6000_0000 has none. peer 0x41 / 0x61 are "
           "the haddr[24]==1 halves (see d2d_links).\n"
           "0x2E is mgr_remap_0 (a live peripheral) on this die, NOT a TideLink "
           "config window — do not reuse the eth die's 0x2E03_0000."),
)

TARGETS: Dict[str, Target] = {
    t.name: t for t in (_ETH_CHIPLET, _COMPUTE_CHIPLET, _BARE_LINK)
}

#: Short aliases accepted by `get_target` / the CLI / hetsoc.toml.
_TARGET_ALIASES = {
    "eth": "kr260-eth-chiplet",
    "eth-chiplet": "kr260-eth-chiplet",
    "kr260_eth": "kr260-eth-chiplet",
    "compute": "kr260-compute-chiplet",
    "compute-chiplet": "kr260-compute-chiplet",
    "bare": "kr260-bare-link",
    "bare-link": "kr260-bare-link",
}


def target_names() -> List[str]:
    """Registered target names, sorted."""
    return sorted(TARGETS)


def get_target(name) -> Target:
    """Look a target up by name (or alias).

    Raises:
        KeyError: unknown name — never a silent fallback to a default map. A
            wrong base on ZynqMP is an unrecoverable bus hang; a loud error is
            free (the rule tl_socmap.py:36-41 established).
    """
    if isinstance(name, Target):
        return name
    key = str(name).strip()
    if key in TARGETS:
        return TARGETS[key]
    alias = _TARGET_ALIASES.get(key.lower())
    if alias and alias in TARGETS:
        return TARGETS[alias]
    raise KeyError(
        "unknown target %r. Known targets: %s (aliases: %s).\n"
        "Refusing to guess an address map."
        % (name, ", ".join(target_names()), ", ".join(sorted(_TARGET_ALIASES))))


def register_target(target: Target, replace: bool = False) -> Target:
    """Add a target to the registry (used by the TOML override path)."""
    if target.name in TARGETS and not replace:
        raise ConfigError("target %r already registered; pass replace=True to "
                          "override it deliberately." % target.name)
    TARGETS[target.name] = target
    return target


# =============================================================================
# Per-design TOML override
#
# CHIPLET_HOST_TOOLING_PLAN.md: "a built-in registry (the three first-party
# targets, kept auditable in one fail-loud table) + an optional per-design TOML
# override for a new SoC (mirrors the fpga/fpgahub.toml per-project-manifest
# precedent)."  This is that override. It is the ONLY supported way to give a
# provisional target a real window — deliberately, so the value lands in a
# reviewable file next to a citation rather than in a script literal.
# =============================================================================
_INT_FIELDS = ("window_base", "window_size", "peer_aperture", "tlapb_base",
               "tidechart_base", "bootrom_soc_base")


def target_from_dict(name: str, spec: Mapping, base: Optional[Target] = None) -> Target:
    """Build a Target from a TOML table, optionally inheriting from *base*.

    ``[target.kr260-compute-chiplet]`` with ``window_base``/``window_size`` is
    how the compute descriptor becomes usable once its bitstream exists.
    """
    if base is None and "inherit" in spec:
        base = get_target(str(spec["inherit"]))
    if base is None and name in TARGETS:
        base = TARGETS[name]

    if base is not None:
        kwargs = dict(
            name=name,
            window_base=base.window_base, window_size=base.window_size,
            inbound_targets=dict(base.inbound_targets),
            peer_aperture=base.peer_aperture, kind=base.kind,
            tlapb_base=base.tlapb_base, tidechart_base=base.tidechart_base,
            bootrom_soc_base=base.bootrom_soc_base,
            bootrom_expect=tuple(base.bootrom_expect),
            apertures=tuple(base.apertures),
            d2d_links=tuple(base.d2d_links),
            provisional=base.provisional,
            provisional_reason=base.provisional_reason,
            deploy_action=base.deploy_action, roles=tuple(base.roles),
            source=base.source, notes=base.notes,
        )
    else:
        kwargs = dict(name=name, window_base=0, window_size=0,
                      inbound_targets={}, peer_aperture=NO_PEER_APERTURE)

    unknown = set(spec) - set(kwargs) - {"inherit"}
    if unknown:
        raise ConfigError(
            "target %r: unknown key(s) %s in hetsoc.toml. Known keys: %s"
            % (name, ", ".join(sorted(unknown)), ", ".join(sorted(kwargs))))

    for key, value in spec.items():
        if key == "inherit":
            continue
        if key in _INT_FIELDS:
            kwargs[key] = _coerce_int(name, key, value)
        elif key == "inbound_targets":
            kwargs[key] = {str(k): _coerce_int(name, "inbound_targets.%s" % k, v)
                           for k, v in dict(value).items()}
        elif key == "bootrom_expect":
            kwargs[key] = tuple(_coerce_int(name, "bootrom_expect", v) for v in value)
        elif key == "apertures":
            kwargs[key] = tuple(
                (str(a[0]), _coerce_int(name, "apertures", a[1]),
                 _coerce_int(name, "apertures", a[2]),
                 _coerce_int(name, "apertures", a[3])) for a in value)
        elif key == "roles":
            kwargs[key] = tuple(str(r) for r in value)
        elif key == "provisional":
            kwargs[key] = bool(value)
        else:
            kwargs[key] = value

    # Supplying a real window is exactly what de-provisionalises a target — but
    # only if the caller also updated the provenance, so the value is auditable.
    if kwargs.get("window_size") and kwargs.get("provisional") \
            and "provisional" not in spec:
        if not str(kwargs.get("source", "")).strip() or "TBD" in str(kwargs.get("source", "")):
            raise ConfigError(
                "target %r: hetsoc.toml gives it a window (base=0x%X size=0x%X) "
                "but leaves `source` as %r. A window base that can wedge a board "
                "must cite where it came from (e.g. the .hwh MEMRANGE line). Set "
                "`source = \"...\"` in the same table."
                % (name, kwargs["window_base"], kwargs["window_size"],
                   kwargs.get("source")))
        kwargs["provisional"] = False
        kwargs["provisional_reason"] = ""

    return Target(**kwargs)


def _coerce_int(target_name: str, key: str, value) -> int:
    """Accept 0x-hex strings as well as TOML integers (hex reads better here)."""
    if isinstance(value, bool):
        raise ConfigError("target %r: %s must be an integer, got a bool"
                          % (target_name, key))
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.replace("_", ""), 0)
        except ValueError:
            pass
    raise ConfigError("target %r: %s = %r is not an integer address."
                      % (target_name, key, value))


def load_target_overrides(spec: Mapping, into: Optional[Dict[str, Target]] = None
                          ) -> Dict[str, Target]:
    """Apply a ``[target.*]`` TOML section to a registry copy (or ``TARGETS``)."""
    registry = TARGETS if into is None else into
    for name, table in dict(spec).items():
        registry[name] = target_from_dict(str(name), dict(table))
    return registry


def snapshot() -> Dict[str, Target]:
    """Deep copy of the registry — for tests that mutate it."""
    return copy.deepcopy(TARGETS)


def all_targets() -> Iterable[Target]:
    """Iterate every registered target."""
    return tuple(TARGETS[n] for n in target_names())
