"""Test-side helpers: only what `hetsoc` does not already provide.

Owned by the **Tests** area (`tests/**`). The framework carries the register
offsets, decoders, health sampling and address maths — this module deliberately
does NOT duplicate them. What lives here is:

  * register offsets for blocks the framework has no reason to model yet (the
    CAM's ARM ID registers, the IPC mailbox PERIPH_ID, TideChart control values,
    PHC and DMA-250 — used only by L1 identity probes and the L6 blocked tests);
  * a pure-Python model of `tl_addr_trans_cam.sv`, so L0 can verify the
    heterogeneous mapping with no hardware;
  * deterministic polling (`poll_until`) — no bare sleeps anywhere in the suite.

Every address is composed the framework's way: offsets are TLAPB- or
block-relative and the base is per-target, via `board.reg()` /
`target.tidechart()` / `target.peer()` / `target.inbound_soc_base()`. Nothing
here constructs a raw PS address.

Copyright (C) 2026, SoC Labs (www.soclabs.org)
"""
from __future__ import annotations

import time

from hetsoc import regs, safety

# ---------------------------------------------------------------------------
# Wlink bank — the one bit the framework does not name
# ---------------------------------------------------------------------------
#: WLINK_LINK_STATUS[2] `in_error_state` (the former `d2d_reset` pad). TIED 0 by
#: a deliberate upstream ECC bypass: `WlinkEccSyndrome.v:306-308` forces
#: `corrupted = 0`, which makes the RX link layer's ERROR state unreachable.
#: docs/STATUS_REGISTERS.md §4. L1-PROBE-06 pins it.
WLINK_IN_ERROR_STATE = 1 << 2

#: ROLE_STATUS is mirrored 0x1000 up: `APB_ADDR_W = 12` drops paddr[12] inside
#: the TideLink region, so 0x2E032084 and 0x2E033084 are the same register.
#: The Wlink region does NOT share the alias. docs/STATUS_REGISTERS.md §6 trap 2.
ROLE_STATUS_MIRROR = regs.ROLE_STATUS + 0x1000

ROLE_STATUS_EFFECTIVE = 1 << 0   # 0 == master (INVERTED — §6 trap 1)
ROLE_STATUS_LOCKED = 1 << 1      # role_locked; also the sole driver of link_active

# ---------------------------------------------------------------------------
# Address-translator CAM — identity registers and the field mask
# ---------------------------------------------------------------------------
#: RULE_n retains only [0] enable, [15:8] match, [23:16] replace.
#: `tl_addr_trans_regs.sv:128-157`.
CAM_RULE_VALID_MASK = 0x00FFFF01

#: The reserved gap 0x030..0xFCC reads this. `tl_addr_trans_regs.sv:190`.
CAM_GAP = regs.CAM_BASE + 0x030
CAM_GAP_MAGIC = 0xCAFECAFE

#: ARM CoreSight-style peripheral/component IDs — a non-fakeable "am I really
#: talking to the CAM?" probe. Matters most on a heterogeneous pair, where the
#: TideLink APB base differs per die and a wrong base reads plausible garbage.
CAM_PIDR = [regs.CAM_BASE + 0xFE0 + 4 * i for i in range(4)]
CAM_PIDR_EXPECT = [0x59, 0x16, 0x15, 0x00]
CAM_CIDR = [regs.CAM_BASE + 0xFF0 + 4 * i for i in range(4)]
CAM_CIDR_EXPECT = [0x50, 0x51, 0x4C, 0x54]

# ---------------------------------------------------------------------------
# IPC mailbox — offsets the framework does not carry
# nanosoc_multicore_addrmap.h:99-109. The compute chiplet uses the SAME offsets
# at its own base (compute_mem.h:46-60); only the base byte differs (0x2A/0x23).
# ---------------------------------------------------------------------------
IPC_IRQ_ENABLE = 0x02C
IPC_LOCK = 0x030
IPC_PERIPH_ID = 0x034
IPC_PERIPH_ID_EXPECT = 0xC0DE0001

# ---------------------------------------------------------------------------
# TideChart control values (docs/TIDECHART_TEST_PLAN.md)
# ---------------------------------------------------------------------------
TC_CTRL_ELECTION_START = 1 << 0
TC_CTRL_ENUM_START = 1 << 1
TC_CTRL_RESET = 1 << 3
TC_TIMEOUT_RESET = 0x03E80100   # POR value
#: G-TMO: the 256-cycle default election timeout is shorter than a D2D
#: round-trip, so an election started at the default ALWAYS times out.
TC_TIMEOUT_WIDE = 0x40008000
TC_LOCAL_ID_UNASSIGNED = 0x1F


def decode_tc_status(raw: int) -> dict:
    """TC_STATUS @ TideChart+0x00."""
    return {"raw": raw,
            "election_done": raw & 1,
            "is_root": (raw >> 1) & 1,
            "enum_done": (raw >> 2) & 1,
            "local_id": (raw >> 3) & 0x1F,
            "total": (raw >> 8) & 0x1F}


# ---------------------------------------------------------------------------
# PTP / PHC and DMA-250 — used only by the L6 blocked tests
# ---------------------------------------------------------------------------
# TideLink bank, tidelink docs/REGISTER_MAP.md:105,133-135.
#: The RETURNER's landing register: a far-die DOORBELL write lands here as a
#: saturating add of the sender's free-credit count. NOT `regs.REL_ACC` (0x018,
#: this die's own pending unreleased credits) — different register, different
#: direction. docs/CROSS_DIE_INTERRUPTS.md mechanism 2.
DOORBELL_RESPONSE_ACC = regs.TIDELINK_BANK + 0x024
PTP_CTRL = regs.TIDELINK_BANK + 0x034        # [0] enable, [2] rx_valid
PTP_CTRL_ENABLE = 1 << 0
HW_SYNC_CTRL = regs.TIDELINK_BANK + 0x040    # [0] enable, [1] seq_clear, [2] force
HW_SYNC_INTERVAL = regs.TIDELINK_BANK + 0x044
HW_SYNC_STATUS = regs.TIDELINK_BANK + 0x048

# phc_apb_regs.rdl. Eth chiplet phc_0 @ 0x2200_0000; compute phc_0 @ 0x2B00_0000.
PHC_BASES = {"kr260-eth-chiplet": 0x22000000,
             "kr260-compute-chiplet": 0x2B000000}
PHC_CTRL = 0x000                 # [2] SW_CAPTURE
PHC_STATUS = 0x004               # [0] RUNNING
PHC_CAP_SECONDS_LO = 0x020
PHC_CAP_SECONDS_HI = 0x024
PHC_CAP_NANOSECONDS = 0x028
PHC_HW_CAP_SECONDS_LO = 0x040    # D2D servo-0 hardware capture
PHC_HW_CAP_SECONDS_HI = 0x044
PHC_HW_CAP_NANOSECONDS = 0x048
PHC_SERVO_CTRL = 0x0A0
PHC_SYNC_INTERVAL = 0x0A4
PHC_SERVO_STATUS = 0x0A8
PHC_CTRL_SW_CAPTURE = 1 << 2
PHC_STATUS_RUNNING = 1 << 0
NS_PER_S = 1000000000

# dma250_driver.h:65-118. Eth chiplet dmac_0 @ 0x2000_0000, channel stride 0x100.
DMAC_BASES = {"kr260-eth-chiplet": 0x20000000}
DMAC_CH0_OFFSET = 0x1000
DMA_CH_CMD = 0x000               # [0] ENABLECMD
DMA_CH_STATUS = 0x004
DMA_CH_CTRL = 0x00C
DMA_CH_SRCADDR = 0x010
DMA_CH_DESADDR = 0x018
DMA_CH_XSIZE = 0x020             # [15:0] SRC, [31:16] DES
DMA_CH_ERRINFO = 0x090
DMA_CMD_ENABLE = 1 << 0

# ---------------------------------------------------------------------------
# Transfer geometry
# ---------------------------------------------------------------------------
#: `shared_sram_0` is 8 KB on both chiplets, not the 16 MB the CAM's byte
#: granularity implies. Every cross-die offset must stay inside it.
SHARED_SRAM_SIZE = 0x2000
#: The offset the on-silicon scripts use (kr260_eth_xfer.py:49-50).
XFER_OFFSET = 0x1000
#: The window the soak cycles.
XFER_WINDOW_WORDS = 16

#: Distinctive, non-zero poison. Zero is useless as a poison here: 0x00000000 is
#: the signature of the peer-write data-phase drop the two-SoC sim found (the
#: address crosses, the data arrives as zero), so a test must be able to tell
#: "never written" from "written as zero".
POISON = 0xDEADBE00


# ---------------------------------------------------------------------------
# A pure-Python model of tl_addr_trans_cam.sv (L0 — no hardware)
# ---------------------------------------------------------------------------
def translate(rule_value: int, addr: int, base_offset: int = 0,
              global_enable: bool = True) -> int:
    """Model one CAM rule, exactly as the RTL implements it.

    docs/PEER_APERTURE_PROGRAMMING.md §2::

        addr_norm     = addr - base_offset
        match         = rule_enable & (rule_match == addr_norm[31:24])
        addr_o[23:0]  = addr[23:0]        <- the RAW low bits, not normalised
        addr_o[31:24] = rule_replace on a match, else addr_norm[31:24]
    """
    addr_norm = (addr - base_offset) & 0xFFFFFFFF
    upper = (addr_norm >> 24) & 0xFF
    enable = rule_value & 1
    match = (rule_value >> 8) & 0xFF
    replace = (rule_value >> 16) & 0xFF
    if global_enable and enable and match == upper:
        return (replace << 24) | (addr & 0xFFFFFF)
    return (upper << 24) | (addr & 0xFFFFFF)


def cam_rule_between(src_target, dst_target, which="shared_sram") -> int:
    """The RULE_0 word mapping `src`'s peer aperture onto `dst`'s inbound region.

    The replace byte comes from the **destination** — that asymmetry is the whole
    heterogeneous problem: eth->eth mailbox is 0x2F->0x23 but eth->compute is
    0x2F->0x2A, because the compute chiplet's mailbox is at 0x2A (its 0x22-0x23
    is the Cortex-M4 SRAM bit-band alias and is deliberately unmapped).
    """
    return regs.cam_rule(src_target.peer_aperture, dst_target.inbound_byte(which))


# ---------------------------------------------------------------------------
# CAM helpers for tests that must control the sequence themselves
# ---------------------------------------------------------------------------
def clear_cam(board) -> None:
    """Drive the translator back to its reset/identity state.

    The CAM resets on `hresetn`, not on POR, so nothing clears it between tests —
    putting it back has to be an explicit, verified operation.
    """
    board.reg_write(regs.CAM_CTRL, 0)
    for index in range(regs.CAM_NUM_RULES):
        board.reg_write(regs.cam_rule_offset(index), 0)
    board.reg_write(regs.CAM_BASE, 0)


def snapshot_cam(board) -> dict:
    return {"base": board.reg_read(regs.CAM_BASE),
            "ctrl": board.reg_read(regs.CAM_CTRL),
            "rules": [board.reg_read(regs.cam_rule_offset(i))
                      for i in range(regs.CAM_NUM_RULES)]}


def restore_cam(board, saved: dict) -> None:
    """Put a snapshot back, CTRL armed last so a half-configured rule is never
    live."""
    board.reg_write(regs.CAM_CTRL, 0)
    for index, value in enumerate(saved["rules"]):
        board.reg_write(regs.cam_rule_offset(index), value)
    board.reg_write(regs.CAM_BASE, saved["base"])
    board.reg_write(regs.CAM_CTRL, saved["ctrl"])


# ---------------------------------------------------------------------------
# Deterministic polling — never sleep-and-hope
# ---------------------------------------------------------------------------
class PollTimeout(AssertionError):
    """Raised by `poll_until`. An AssertionError, so pytest reports it as a
    failure carrying the observation history rather than as an error."""


def poll_until(sample, predicate, timeout_s=5.0, interval_s=0.02,
               what="condition"):
    """Poll `sample()` until `predicate(value)`; return the value.

    Raises `PollTimeout` naming what was expected, how long it waited, how many
    times it looked, and the last value seen. Every wait in this suite has a
    deadline and a message; none of them is a bare sleep.
    """
    deadline = time.monotonic() + timeout_s
    count = 0
    while True:
        value = sample()
        count += 1
        if predicate(value):
            return value
        if time.monotonic() >= deadline:
            raise PollTimeout(
                "%s never became true within %.2fs (%d polls); last value = %s"
                % (what, timeout_s, count,
                   ("0x%08X" % value) if isinstance(value, int) else repr(value)))
        time.sleep(interval_s)


def call_guarded(timeout_s, fn, *args, **kwargs):
    """Run `fn` under `hetsoc.safety.guarded` so a bus hang surfaces as
    `WedgeDetected` instead of blocking the session forever."""
    return safety.guarded(timeout_s)(fn)(*args, **kwargs)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def fmt_health(sample: dict) -> str:
    """One-line summary of a `hetsoc.health.link_health()` sample, for assertion
    messages."""
    lane = sample.get("lane_status", {})
    return ("%s(%s/%s): fcsm=%s(%s) cal=%s lane_fault=0x%02X sticky=0x%X "
            "credits=%s cr/crack=%s/%s sync_det=%s"
            % (sample.get("board"), sample.get("role"), sample.get("target"),
               sample.get("fcsm"), sample.get("fcsm_name"),
               sample.get("cal_done"), sample.get("lane_fault", 0),
               sample.get("sticky", 0), sample.get("credit_count"),
               lane.get("cr_seen"), lane.get("crack_seen"),
               sample.get("sync_detected")))
