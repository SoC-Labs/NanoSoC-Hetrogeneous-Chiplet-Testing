# =============================================================================
# hetsoc.regs — SoC-internal TideLink/TideChart register offsets.
#
# WHY THESE ARE OFFSETS, NOT ABSOLUTE ADDRESSES
# --------------------------------------------
# CHIPLET_HOST_TOOLING_PLAN.md's central finding: "base is per-target; offset is
# shared". The eth-chiplet and the bare-link design differ *entirely* in the
# base/access layer; every register offset inside TideLink is identical because
# it is the same RTL. So a tool composes:
#
#     target.to_host(regs.TLAPB_BASE + regs.SWI_LANE_STATUS)
#            \__ per-target guard __/   \__ shared, this file __/
#
# ...which is verbatim the shape that plan recommends (§"The address-map
# abstraction"). `TLAPB_BASE` is the SoC HADDR of the TideLink APB region for
# the nanoSoC chiplet family; a design that moves it overrides it in its target
# descriptor (`Target.tlapb_base`), and nothing else changes.
#
#     >>> ABSOLUTE-vs-OFFSET TRAP <<<
# `SWI_LANE_STATUS` is 0x2108, NOT 0x2E032108. Passing a bare offset to
# `Board.read()` reads a *different, in-window* address — harmless (the SoC AHB
# matrix's default slave SLVERRs, it cannot hang) but silently wrong. The
# `SOC_*` aliases at the bottom are the pre-composed absolute forms for the
# default base; prefer `board.reg(regs.SWI_LANE_STATUS)`, which cannot be
# ambiguous.
#
# PROVENANCE
# ----------
# Offsets mirror the packaged tidelink register map
# (`tidelink/python/tidelink/regs.py`) and the on-silicon-proven scripts
# `kr260_eth_bringup.py` / `kr260_eth_xfer.py`. Where the two disagreed the
# RTL-backed value in docs/PEER_APERTURE_PROGRAMMING.md wins, and the reason is
# cited inline. Nothing in this file is inferred; every constant has a source.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""Shared SoC-internal register offsets, bit decoders and the CAM rule encoder."""

from __future__ import annotations

from typing import Dict, List, Tuple

__all__ = [
    "TLAPB_BASE", "TIDECHART_BASE",
    "WLINK_BANK", "TIDELINK_BANK", "ADDRXLAT_BANK",
    "WL_LINK_ENABLE_RESET", "WLINK_LINK_STATUS",
    "PAIR_BASE_ADDR", "REL_THRESHOLD", "PKT_WORD_LEN", "CREDIT_COUNT", "STATUS",
    "DOORBELL", "REL_ACC", "CTRL", "PAIR_CREDIT_COUNTER",
    "ROLE_CFG", "ROLE_STATUS", "SERVO_STATUS",
    "SWI_TRAINING_MODE", "SWI_LANE_STATUS", "SYNC_DET", "OBS_FC_CREDIT",
    "PERF_CTRL", "PERF_CONG_STATE", "PERF_ID", "PERF_ID_EXPECT",
    "CAM_BASE", "CAM_CTRL", "CAM_RULE_0", "CAM_RULE_STRIDE", "CAM_NUM_RULES",
    "FCSM_LINK_IDLE", "FCSM_NAMES",
    "ROLE_CFG_MASTER_LOCK", "ROLE_CFG_SLAVE_LOCK", "ROLE_LOCK",
    "LL_SWRESET_ON", "LL_SWRESET_OFF", "LL_ENABLE",
    "STATUS_STICKY_MASK", "STATUS_BITS", "CREDIT_COUNT_MASK",
    "FC_NODES", "FC_AXI_DATA_NODES", "FC_TXFIFO", "FC_ACKNACK", "FC_CRC",
    "IPC_SLOT0_DATA", "IPC_SLOT0_CTRL", "IPC_IRQ_STATUS",
    "IPC_MSG_VALID", "IPC_ACK", "IPC_SLOT0_NWORDS",
    "TC_STATUS", "TC_BEST_CLAIM", "TC_CTRL", "TC_TIMEOUT", "TC_DEVICE_CLASS",
    "TC_ROUTE_RD", "TC_RANDOM_ID", "TC_UPLINK_PORT", "TC_PORT_COUNT",
    "TC_ENUM_STATE", "TC_ERROR", "TC_CONG_CTRL", "TC_CONG_STATUS",
    "LaneStatus", "decode_lane_status", "cam_rule", "decode_cam_rule",
    "cam_rule_offset", "decode_sticky_status", "fc_node_regs", "all_offsets",
    "SOC_SWI_LANE_STATUS", "SOC_ROLE_CFG", "SOC_ROLE_STATUS",
    "SOC_CAM_BASE", "SOC_CAM_CTRL", "SOC_CAM_RULE_0",
]

# =============================================================================
# Bank bases
# =============================================================================
#: SoC HADDR of the TideLink APB region, reached through the chiplet-decode
#: `tlapb` bridge. `kr260_eth_bringup.py:74`; docs/STATUS_REGISTERS.md §7.
TLAPB_BASE = 0x2E030000

#: SoC HADDR of the TideChart (identity/election/routing) APB register file.
#: `kr260_tidechart.py:35`; docs/TIDECHART_TEST_PLAN.md.
TIDECHART_BASE = 0x2E040000

# `apb_paddr[14:13]` selects the bank inside tidelink_top
# (tidelink_top.sv:683-690; docs/PEER_APERTURE_PROGRAMMING.md §3).
WLINK_BANK = 0x0000       # bank 00: Wlink chiplet-controller regs (8 KB)
TIDELINK_BANK = 0x2000    # bank 01: TideLink config + status + PTP (8 KB)
ADDRXLAT_BANK = 0x4000    # bank 10: address-translator (CAM) config, channel 0

# =============================================================================
# Bank 00 — Wlink chiplet controller
# =============================================================================
#: Wlink LL enable/reset. The 3-write bootstrap below is written here.
WL_LINK_ENABLE_RESET = WLINK_BANK + 0x0208      # SoC 0x2E030208

#: Wlink link status. [3] TX lanes active, [4] RX data valid.
#: [2] in_error_state is TIED 0 by a deliberate upstream ECC bypass — do NOT
#: gate on it (docs/STATUS_REGISTERS.md §4).
WLINK_LINK_STATUS = WLINK_BANK + 0x0234         # SoC 0x2E030234
WLINK_TX_LANES_ACTIVE = 1 << 3
WLINK_RX_DATA_VALID = 1 << 4

# =============================================================================
# Bank 01 — TideLink config / status  (mirrors tidelink/python/tidelink/regs.py,
# rebased onto the unified APB: tidelink regs.py offsets + TIDELINK_BANK)
# =============================================================================
PAIR_BASE_ADDR = TIDELINK_BANK + 0x000    # RW peer TideLink base (see PEER_APERTURE §7)
REL_THRESHOLD = TIDELINK_BANK + 0x004     # RW credit release threshold
PKT_WORD_LEN = TIDELINK_BANK + 0x008      # RO
CREDIT_COUNT = TIDELINK_BANK + 0x00C      # RO local free credits  -> SoC 0x2E03200C
STATUS = TIDELINK_BANK + 0x010            # RO sticky faults       -> SoC 0x2E032010
DOORBELL = TIDELINK_BANK + 0x014          # W1C software doorbell
REL_ACC = TIDELINK_BANK + 0x018           # RO pending unreleased credits
CTRL = TIDELINK_BANK + 0x01C              # RW [1]FLUSH [2]LOCK [3]AHB_INJECT_ARM
PAIR_CREDIT_COUNTER = TIDELINK_BANK + 0x028   # RO — reads 0 on a HEALTHY link

#: RW role select + lock. bit[0] role (0=master, 1=slave), bit[1] role_lock.
#: 0x2080 — NOT 0x2084. INTEGRATION_GUIDE.md says 0x2084; that is wrong and the
#: role silently never locks (docs/PEER_APERTURE_PROGRAMMING.md §6 note).
ROLE_CFG = TIDELINK_BANK + 0x080          # SoC 0x2E032080
#: RO effective role. bit[0] effective_role (0=master), bit[1] role_locked.
#: NOTE bit[0] is INVERTED w.r.t. "is master" (docs/STATUS_REGISTERS.md §6.1).
ROLE_STATUS = TIDELINK_BANK + 0x084       # SoC 0x2E032084
SERVO_STATUS = TIDELINK_BANK + 0x05C      # RO TideLink PTP servo (NOT ha1588)

SWI_TRAINING_MODE = TIDELINK_BANK + 0x100     # W  drop training mode: write 0
SWI_LANE_STATUS = TIDELINK_BANK + 0x108       # RO -> SoC 0x2E032108
SYNC_DET = TIDELINK_BANK + 0x114              # RO [31:16] sync_detected_cnt
OBS_FC_CREDIT = TIDELINK_BANK + 0x19C         # RO far-end credit observation

# Perf/telemetry. Region-decode off-by-one FIXED 2026-07-16; the EWMA still
# reads 0 until PERF_CTRL[0] is set (docs/STATUS_REGISTERS.md §5).
PERF_CTRL = TIDELINK_BANK + 0x0A0
PERF_CONG_STATE = TIDELINK_BANK + 0x0F8       # [12:0] EWMA credit
PERF_ID = TIDELINK_BANK + 0x0FC
PERF_ID_EXPECT = 0x50460100

# =============================================================================
# Bank 10 — address-translator CAM (channel 0)
# All from docs/PEER_APERTURE_PROGRAMMING.md §2-§3 (RTL-authoritative:
# tl_addr_trans_regs.sv), cross-checked against kr260_eth_xfer.py:45-47.
# =============================================================================
CAM_BASE = ADDRXLAT_BANK + 0x000          # RW BASE_OFFSET -> SoC 0x2E034000
CAM_CTRL = ADDRXLAT_BANK + 0x004          # RW [0] global_enable -> SoC 0x2E034004
CAM_RULE_0 = ADDRXLAT_BANK + 0x010        # RW rule 0 (highest priority)
CAM_RULE_STRIDE = 0x004
CAM_NUM_RULES = 8                          # NUM_RULES default 8, one channel only

# =============================================================================
# Constants
# =============================================================================
#: The link-up criterion. FCSM == 4 == LINK_IDLE, bilaterally, with cal_done=1.
FCSM_LINK_IDLE = 4

FCSM_NAMES = {
    0: "RESET", 1: "TRAINING", 2: "WAIT_CRACK", 3: "SEND_CRACK",
    4: "LINK_IDLE", 5: "STATE_5", 6: "STATE_6", 7: "STATE_7",
}

ROLE_LOCK = 0x02
ROLE_CFG_MASTER_LOCK = 0x02   # role_lock=1, role=0 -> master/grandmaster (die_a)
ROLE_CFG_SLAVE_LOCK = 0x03    # role_lock=1, role=1 -> slave              (die_b)

# 3-write LL bootstrap into WL_LINK_ENABLE_RESET. Order matters: swreset first
# clears the CR/CRACK stickies. Verbatim from test_g2_soc_pair.py via
# kr260_eth_bringup.py:91-93.
LL_SWRESET_ON = 0x00027F08
LL_SWRESET_OFF = 0x00027F00
LL_ENABLE = 0x00027F07

#: STATUS sticky-fault bits [3:1]: OVERRUN, UNDERRUN, MASTER_ERROR.
STATUS_STICKY_MASK = 0xE
STATUS_BITS = (
    (0, "returner_busy", False),
    (1, "overrun", True),
    (2, "underrun", True),
    (3, "master_error", True),
    (4, "packet_committed", False),
    (5, "ahb_inject_fault", False),
)
CREDIT_COUNT_MASK = 0x1FFF        # 13-bit local free-credit count

# =============================================================================
# Per-node Wlink flow-control health — THE diagnostic for the known wedge.
#
# docs/CROSS_DIE_WEDGE_ROOTCAUSE.md: the silicon build ships the UPSTREAM
# (recovery-stripped) FCSM on the five AXI data-plane nodes; only the TideLink
# sideband node keeps the SoC-Labs recovery logic. A single bit error or dropped
# ACK on an AXI node therefore has no recovery path and wedges the PS bus.
# CRUCIALLY: OBS_FC_CREDIT and SWI_LANE_STATUS[31:17] observe the *sideband*
# node only — they do NOT see the nodes that wedge. These per-node registers
# are the only visibility there is. Poll them BETWEEN transfers.
#
# Node bases are offsets from TLAPB_BASE; per-node regs at +0x08/+0x10/+0x20.
# Table verbatim from kr260_eth_xfer.py:84-88 (REGISTER_MAP.md §2.3).
# =============================================================================
FC_NODES: Tuple[Tuple[str, int], ...] = (
    ("AW", 0x1000), ("W", 0x1100), ("B", 0x1200), ("AR", 0x1300),
    ("R", 0x1400), ("GenBus", 0x1600), ("TideLink", 0x1700),
)
#: The recovery-stripped ones. A non-empty Ack/Nack FIFO here is the wedge
#: signature; on GenBus/TideLink it is not.
FC_AXI_DATA_NODES = ("AW", "W", "B", "AR", "R")

FC_TXFIFO = 0x08      # [0] TX FC FIFO empty
FC_ACKNACK = 0x10     # [0]empty [1]full [2]halffull [3]almostempty [4]almostfull
FC_CRC = 0x20         # [15:0] CRC error count (RO)

# =============================================================================
# IPC mailbox (ipc_mailbox_0) — the SECOND inbound D2D target.
# Layout from nanosoc_multicore_addrmap.h via kr260_eth_xfer.py:64-68.
# Offsets are relative to the mailbox base (a *target* base, not TLAPB).
# =============================================================================
IPC_SLOT0_DATA = 0x000        # .. 0x00C, 4 words
IPC_SLOT0_CTRL = 0x020        # [0] MSG_VALID, [1] ACK
IPC_IRQ_STATUS = 0x028        # [0] slot0 msg edge -> CPU1 NVIC IRQ0
IPC_MSG_VALID = 1 << 0
IPC_ACK = 1 << 1
IPC_SLOT0_NWORDS = 4

# =============================================================================
# TideChart register file (offsets from TIDECHART_BASE).
# From tidechart_apb_regs.sv via kr260_tidechart.py:39-52.
# =============================================================================
TC_STATUS = 0x00        # [0]election_done [1]is_root [2]enum_done [7:3]id [12:8]total
TC_BEST_CLAIM = 0x04
TC_CTRL = 0x08          # [0]election_start [1]enum_start [3]reset
TC_TIMEOUT = 0x0C
TC_DEVICE_CLASS = 0x10
TC_ROUTE_RD = 0x18
TC_RANDOM_ID = 0x1C
TC_UPLINK_PORT = 0x20
TC_PORT_COUNT = 0x24
TC_ENUM_STATE = 0x28
TC_ERROR = 0x2C
TC_CONG_CTRL = 0x74
TC_CONG_STATUS = 0x78


# =============================================================================
# Decoders / encoders
# =============================================================================
class LaneStatus:
    """Decoded ``SWI_LANE_STATUS`` (SoC 0x2E032108).

    Bit layout from ``kr260_eth_bringup.py:decode_status`` (which matches
    ``test_g2_soc_pair.py``)::

        [7:0]   lane_locked   — reads 0x00 after training. NOT a health signal.
        [15:8]  lane_fault
        [16]    calibration_done
        [19:17] fcsm          — 4 == LINK_IDLE == up
        [23]    cr_seen
        [24]    crack_seen
    """

    __slots__ = ("raw", "lane_locked", "lane_fault", "cal_done", "fcsm",
                 "cr_seen", "crack_seen")

    def __init__(self, raw: int) -> None:
        self.raw = raw & 0xFFFFFFFF
        self.lane_locked = self.raw & 0xFF
        self.lane_fault = (self.raw >> 8) & 0xFF
        self.cal_done = (self.raw >> 16) & 1
        self.fcsm = (self.raw >> 17) & 0x7
        self.cr_seen = (self.raw >> 23) & 1
        self.crack_seen = (self.raw >> 24) & 1

    @property
    def link_up(self) -> bool:
        """True iff FCSM==4 (LINK_IDLE) AND calibration_done==1.

        This is the ONLY link-up criterion used anywhere in this framework.
        Do not substitute lane_locked (self-deasserts after training) or
        ``link_active`` (== role_locked, docs/STATUS_REGISTERS.md §3).
        """
        return self.fcsm == FCSM_LINK_IDLE and self.cal_done == 1

    @property
    def fcsm_name(self) -> str:
        return FCSM_NAMES.get(self.fcsm, "FCSM_%d" % self.fcsm)

    def as_dict(self) -> Dict[str, int]:
        return {
            "raw": self.raw, "lane_locked": self.lane_locked,
            "lane_fault": self.lane_fault, "cal_done": self.cal_done,
            "fcsm": self.fcsm, "cr_seen": self.cr_seen,
            "crack_seen": self.crack_seen, "link_up": int(self.link_up),
        }

    def __eq__(self, other: object) -> bool:
        return isinstance(other, LaneStatus) and other.raw == self.raw

    def __hash__(self) -> int:
        return hash(self.raw)

    def __repr__(self) -> str:
        return ("LaneStatus(raw=0x%08X, fcsm=%d(%s), cal_done=%d, "
                "lane_locked=0x%02X, lane_fault=0x%02X, cr=%d, crack=%d, up=%s)"
                % (self.raw, self.fcsm, self.fcsm_name, self.cal_done,
                   self.lane_locked, self.lane_fault, self.cr_seen,
                   self.crack_seen, self.link_up))


def decode_lane_status(v: int) -> LaneStatus:
    """Decode a raw ``SWI_LANE_STATUS`` word into a :class:`LaneStatus`."""
    return LaneStatus(v)


def cam_rule(match_byte: int, replace_byte: int, enable: bool = True) -> int:
    """Encode one address-translator CAM rule word.

    Layout ``[0]=enable, [15:8]=match, [23:16]=replace`` — RTL-authoritative
    from ``tl_addr_trans_regs.sv:128-157`` via
    docs/PEER_APERTURE_PROGRAMMING.md §2. The CAM matches and replaces
    ``addr[31:24]`` only, passing ``addr[23:0]`` through unchanged, so one rule
    is one 16 MB-aligned source->dest mapping.

    Proven values: ``cam_rule(0x2F, 0x2D) == 0x002D2F01`` (peer aperture ->
    far-die ``shared_sram_0``) and ``cam_rule(0x2F, 0x23) == 0x00232F01``
    (-> far-die ``ipc_mailbox_0``).

    Raises ``ValueError`` on a non-byte, because a truncated match byte would
    silently arm a rule that routes traffic to the wrong far-die region (which
    DECERRs on the far side — the far die's inbound initiator reaches exactly
    two targets).
    """
    for name, val in (("match_byte", match_byte), ("replace_byte", replace_byte)):
        if not isinstance(val, int) or isinstance(val, bool):
            raise ValueError("cam_rule: %s must be an int, got %r" % (name, val))
        if not 0 <= val <= 0xFF:
            raise ValueError(
                "cam_rule: %s=0x%X is not an 8-bit address byte (0x00..0xFF). "
                "The CAM matches/replaces addr[31:24] only." % (name, val))
    return ((replace_byte & 0xFF) << 16) | ((match_byte & 0xFF) << 8) | (1 if enable else 0)


def decode_cam_rule(word: int) -> Dict[str, int]:
    """Inverse of :func:`cam_rule` — for readback verification."""
    return {
        "raw": word & 0xFFFFFFFF,
        "enable": word & 1,
        "match": (word >> 8) & 0xFF,
        "replace": (word >> 16) & 0xFF,
    }


def decode_sticky_status(v: int) -> Dict[str, int]:
    """Decode the TideLink ``STATUS`` word (sticky faults live in [3:1])."""
    out: Dict[str, int] = {"raw": v & 0xFFFFFFFF,
                           "sticky": v & STATUS_STICKY_MASK}
    for bit, name, _sticky in STATUS_BITS:
        out[name] = (v >> bit) & 1
    return out


def fc_node_regs(node_base: int) -> Dict[str, int]:
    """TLAPB-relative offsets of one Wlink FC node's three health registers."""
    return {
        "txfifo": node_base + FC_TXFIFO,
        "acknack": node_base + FC_ACKNACK,
        "crc": node_base + FC_CRC,
    }


def cam_rule_offset(index: int) -> int:
    """TLAPB-relative offset of CAM ``RULE_<index>`` (rule 0 = highest priority)."""
    if not 0 <= index < CAM_NUM_RULES:
        raise ValueError("CAM rule index %d out of range 0..%d"
                         % (index, CAM_NUM_RULES - 1))
    return CAM_RULE_0 + index * CAM_RULE_STRIDE


# =============================================================================
# Pre-composed absolute SoC addresses, for the default nanoSoC chiplet TLAPB
# base. Convenience only — `Board.reg()` composes these per-target and is the
# form that cannot be mis-used.
# =============================================================================
SOC_SWI_LANE_STATUS = TLAPB_BASE + SWI_LANE_STATUS   # 0x2E032108
SOC_ROLE_CFG = TLAPB_BASE + ROLE_CFG                 # 0x2E032080
SOC_ROLE_STATUS = TLAPB_BASE + ROLE_STATUS           # 0x2E032084
SOC_CAM_BASE = TLAPB_BASE + CAM_BASE                 # 0x2E034000
SOC_CAM_CTRL = TLAPB_BASE + CAM_CTRL                 # 0x2E034004
SOC_CAM_RULE_0 = TLAPB_BASE + CAM_RULE_0             # 0x2E034010


def all_offsets() -> List[Tuple[str, int]]:
    """(name, value) for every register offset — used by `hetsoc regs` and tests."""
    skip = {"TLAPB_BASE", "TIDECHART_BASE", "FCSM_LINK_IDLE", "PERF_ID_EXPECT",
            "CAM_NUM_RULES", "CAM_RULE_STRIDE", "IPC_SLOT0_NWORDS"}
    g = globals()
    return sorted(
        ((n, g[n]) for n in __all__
         if n not in skip and isinstance(g.get(n), int) and not isinstance(g.get(n), bool)),
        key=lambda kv: kv[1])
