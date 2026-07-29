"""L1 — one board, read-only probes: boot-ROM aliveness, config-plane readback,
role/strap, and block identity.

Every access here is a **read** of an in-window address, which is wedge-safe by
construction: the SoC's AHB matrix has a CMSDK default slave on the free-running
system clock, so an undecoded *in-window* transfer returns HREADY=1 + SLVERR
rather than hanging. The class of hang that wedges a KR260 is an *out-of-window*
PL access, which the target guard refuses (L0-ADDR-04/06).

These tests need **no link**. The TideLink config APB is readable with the link
down: the `tx_open` gate applies only to the TX aperture, and the APB registers
are reset by `hresetn`, not by `role_locked` (docs/STATUS_REGISTERS.md §1). That
is the point — these bits are needed precisely when the link is *not* up.

Every test runs against BOTH dies of the pair (`each_board`).

Copyright (C) 2026, SoC Labs (www.soclabs.org)
"""
from __future__ import annotations

import pytest

from hetsoc import regs
from hetsoc.safety import AddressGuardError

import _helpers as H

pytestmark = [pytest.mark.l1, pytest.mark.hardware, pytest.mark.single_board]


def test_l1_probe_01_backdoor_is_alive(each_board):
    """L1-PROBE-01: the PS->SoC backdoor answers, and the boot ROM reads back its
    known signature.

    Proves the access path before anything else is believed: PS HPM0_FPD high
    aperture -> SoC AHB matrix -> boot ROM. The ROM is combinational with HREADY
    hardwired high on the free-running system clock, so it answers even with both
    cores halted and boot-gated — exactly the state the PS flow leaves them in.
    Pass: `alive()` is True and, where the target carries a signature, the vector
    table words match it exactly.
    """
    board = each_board
    assert board.alive(), (
        "%s: boot-ROM probe failed — the PS cannot reach the SoC. Check the "
        "bitstream is loaded (fpga_manager=operating) and that the AFI PS-master "
        "port widths were re-poked after the load." % board.name)

    expect = board.target.bootrom_expect
    if not expect or board.target.bootrom_soc_base is None:
        pytest.skip("target %r carries no boot-ROM signature — `alive()` passed, "
                    "but there is nothing to compare the vector table against"
                    % board.target.name)
    got = tuple(board.read_many(board.target.bootrom_soc_base, len(expect)))
    assert got == tuple(expect), (
        "%s boot ROM reads %s, expected %s (init MSP, then the reset / NMI / "
        "HardFault vectors). A mismatch means the PS is reading a different "
        "image than this target descriptor describes."
        % (board.name, ["0x%08X" % w for w in got],
           ["0x%08X" % w for w in expect]))


def test_l1_probe_02_config_plane_reads_with_the_link_down(each_board):
    """L1-PROBE-02: SWI_LANE_STATUS is readable and decodes to a legal state,
    whatever the link is doing.

    Proves the config plane is reachable independently of link state — the
    property that makes a down link diagnosable at all. FCSM is a 3-bit field, so
    an out-of-range decode would mean the read landed on the wrong register (or
    the wrong SoC address entirely, which on a heterogeneous pair is a live risk
    because the TideLink APB base differs per die).
    Pass: FCSM in 0..7 with a known name, cal_done in {0,1}, and `link_up`
    exactly equal to (FCSM == LINK_IDLE and cal_done).
    """
    board = each_board
    raw = board.reg_read(regs.SWI_LANE_STATUS)
    status = board.lane_status()

    assert status.raw == raw, (
        "%s: Board.lane_status() read 0x%08X but a direct read of the same "
        "offset gave 0x%08X — the framework and the suite disagree about where "
        "SWI_LANE_STATUS lives" % (board.name, status.raw, raw))
    assert 0 <= status.fcsm <= 7
    assert status.fcsm_name in regs.FCSM_NAMES.values()
    assert status.cal_done in (0, 1)
    assert status.link_up == (status.fcsm == regs.FCSM_LINK_IDLE
                              and status.cal_done == 1), (
        "%s: link_up=%s contradicts fcsm=%d cal_done=%d"
        % (board.name, status.link_up, status.fcsm, status.cal_done))


def test_l1_probe_03_role_strap_is_correct_for_this_die(each_board):
    """L1-PROBE-03: the die's effective role matches the role it was deployed as.

    Proves the two dies are not running the same image. die_a must resolve to
    master (effective_role 0 — the bit is INVERTED) and die_b to slave. Swap the
    images and cal_done never asserts, which presents as a ribbon fault and costs
    a bench session; worse, the same image on both boards drives two outputs onto
    every ribbon lane.
    Pass: `role_status()["role_ok"]`. Skips while role_lock is clear, because
    until ROLE_CFG has been written the effective role is just the POR value.
    """
    board = each_board
    role = board.role_status()
    if not role["role_locked"]:
        pytest.skip(
            "%s: role_lock is clear (ROLE_STATUS=0x%08X), so effective_role is "
            "still the POR value and means nothing. The role is latched by the "
            "ROLE_CFG write during bring-up and clears only on poresetn — run L3 "
            "(or the suite with --deploy) first." % (board.name, role["raw"]))

    assert role["role_ok"], (
        "%s is configured as %s but ROLE_STATUS=0x%08X gives effective_role=%d "
        "(expected %d; the bit is INVERTED, 0 = master). If both dies read the "
        "same value, both boards were flashed with the same image — cal_done "
        "will never assert and every ribbon lane has two drivers."
        % (board.name, board.role, role["raw"], role["effective_role"],
           role["expected_role"]))


def test_l1_probe_04_address_translator_identity_registers(each_board):
    """L1-PROBE-04: the address-translator CAM answers with its ARM
    peripheral-ID signature.

    Proves we are talking to the CAM and not to whatever else happens to occupy
    that SoC address on this target. This is the check that matters most on a
    heterogeneous pair: the TideLink APB base is *not* the same on both dies
    (eth 0x2E03_0000, compute 0x4003_0000, and the latter is derived rather than
    stated anywhere). A wrong base reads plausible garbage; PIDR/CIDR do not.
    Pass: PIDR0-3 == 0x59,0x16,0x15,0x00; CIDR0-3 == 0x50,0x51,0x4C,0x54; the
    reserved gap reads 0xCAFECAFE.
    """
    board = each_board
    pidr = [board.reg_read(off) & 0xFF for off in H.CAM_PIDR]
    cidr = [board.reg_read(off) & 0xFF for off in H.CAM_CIDR]
    gap = board.reg_read(H.CAM_GAP)

    assert pidr == H.CAM_PIDR_EXPECT, (
        "%s: CAM PIDR0-3 = %s, expected %s. The TideLink APB base for target %r "
        "(0x%08X) is probably wrong."
        % (board.name, ["0x%02X" % v for v in pidr],
           ["0x%02X" % v for v in H.CAM_PIDR_EXPECT],
           board.target.name, board.target.tlapb_base))
    assert cidr == H.CAM_CIDR_EXPECT, (
        "%s: CAM CIDR0-3 = %s, expected %s"
        % (board.name, ["0x%02X" % v for v in cidr],
           ["0x%02X" % v for v in H.CAM_CIDR_EXPECT]))
    assert gap == H.CAM_GAP_MAGIC, (
        "%s: the CAM's reserved gap reads 0x%08X, expected 0x%08X"
        % (board.name, gap, H.CAM_GAP_MAGIC))


def test_l1_probe_05_health_sample_is_well_formed(each_board, record_property):
    """L1-PROBE-05: a full health sample reads back legal values on every field
    the later levels gate on.

    Proves the observables exist and decode before anything depends on them.
    CREDIT_COUNT is a 13-bit field; zero free credits with no traffic in flight
    means the read missed the register rather than that the link is starved. The
    per-node FC block must enumerate all seven nodes, because those registers are
    the *only* visibility into the five recovery-stripped AXI nodes that wedge —
    OBS_FC_CREDIT and SWI_LANE_STATUS cannot see them.
    Pass: CREDIT_COUNT in (0, 0x1FFF]; every FC node present and decoded; the
    verdict structure is populated. Absolute values are recorded, not asserted.
    """
    board = each_board
    sample = board.health()

    record_property("%s_health" % board.name, H.fmt_health(sample))
    record_property("%s_credit_count" % board.name, sample["credit_count"])
    record_property("%s_sync_detected" % board.name, sample["sync_detected"])

    assert 0 < sample["credit_count"] <= regs.CREDIT_COUNT_MASK, (
        "%s: CREDIT_COUNT = %d (field mask 0x%X). Zero free credits with nothing "
        "in flight means the read missed the register."
        % (board.name, sample["credit_count"], regs.CREDIT_COUNT_MASK))

    nodes = sample["fc"]["nodes"]
    assert set(nodes) == {name for name, _base in regs.FC_NODES}, (
        "%s: FC health enumerated %s, expected all of %s. The five AXI data "
        "nodes are the recovery-stripped ones that wedge, and these registers "
        "are the only place they are visible."
        % (board.name, sorted(nodes), sorted(n for n, _b in regs.FC_NODES)))
    for name in regs.FC_AXI_DATA_NODES:
        assert nodes[name]["axi_data"] is True, (
            "%s: FC node %s is not flagged as an AXI data node" % (board.name, name))
    assert "ok" in sample["verdict"] and "reasons" in sample["verdict"]


def test_l1_probe_06_in_error_state_is_tied_low(each_board, record_property):
    """L1-PROBE-06: Wlink LINK_STATUS[2] (`in_error_state`, the former
    `d2d_reset` pad) reads 0.

    Pins a documented *tie*, not a health property. The ECC syndrome checker is a
    deliberate bring-up bypass — `WlinkEccSyndrome.v:306-308` forces
    `corrupted = 0` — which makes the RX link layer's ERROR state unreachable, so
    this bit can never be 1. Pinning it means that if a future build restores
    real ECC checking, the suite says so instead of silently changing what
    "healthy" means.
    Pass: bit[2] == 0. tx_lanes_active / rx_data_valid are recorded: for a die
    whose link will not come up they are the most informative bits available,
    because they are the only ones not downstream of role-lock.
    """
    board = each_board
    value = board.reg_read(regs.WLINK_LINK_STATUS)
    record_property("%s_wlink_status" % board.name, "0x%08X" % value)
    record_property("%s_tx_lanes_active" % board.name,
                    int(bool(value & regs.WLINK_TX_LANES_ACTIVE)))
    record_property("%s_rx_data_valid" % board.name,
                    int(bool(value & regs.WLINK_RX_DATA_VALID)))

    assert not (value & H.WLINK_IN_ERROR_STATE), (
        "%s: WLINK_LINK_STATUS=0x%08X has in_error_state set. That bit is "
        "unreachable in this build (the Hamming(33,24) syndrome checker is "
        "bypassed, so the FSM's ERROR entry is dead code). If it is genuinely 1, "
        "this is not the build docs/STATUS_REGISTERS.md §4 describes."
        % (board.name, value))


def test_l1_probe_07_role_status_apb_mirror_aliases(each_board):
    """L1-PROBE-07: ROLE_STATUS is mirrored 0x1000 higher, as the APB decode says.

    Proves the TideLink APB decode is the width the docs claim: `APB_ADDR_W = 12`
    drops paddr[12] inside the TideLink region, so 0x2E032084 and 0x2E033084 are
    the same register. Worth pinning because it is a trap: a tool that
    "helpfully" uses the mirror gets the right answer on TideLink registers and
    the wrong one in the Wlink region, which does *not* share the alias.
    Pass: the two addresses read identically.
    """
    board = each_board
    direct = board.reg_read(regs.ROLE_STATUS)
    mirror = board.reg_read(H.ROLE_STATUS_MIRROR)
    assert direct == mirror, (
        "%s: ROLE_STATUS reads 0x%08X at +0x%04X but 0x%08X at its +0x%04X "
        "mirror. paddr[12] should be a don't-care inside the TideLink region."
        % (board.name, direct, regs.ROLE_STATUS, mirror, H.ROLE_STATUS_MIRROR))


def test_l1_probe_08_ipc_mailbox_is_present_locally(each_board):
    """L1-PROBE-08: `ipc_mailbox_0` — the second inbound D2D target — answers
    locally with its peripheral ID.

    Proves the mailbox exists at the byte this target's descriptor claims,
    *before* L4 tries to reach it across the link. This is the check that catches
    the heterogeneous trap early: the eth chiplet's mailbox is at 0x23 but the
    compute chiplet's is at 0x2A (0x22-0x23 is that die's Cortex-M4 bit-band
    alias and is deliberately unmapped), so a CAM rule copied from the eth flow
    would DECERR at a compute die with no local evidence why.
    Pass: PERIPH_ID reads 0xC0DE0001 at `inbound_soc_base("ipc_mailbox")`.
    """
    board = each_board
    base = board.target.inbound_soc_base("ipc_mailbox")
    periph_id = board.read(base + H.IPC_PERIPH_ID)
    assert periph_id == H.IPC_PERIPH_ID_EXPECT, (
        "%s: ipc_mailbox_0 PERIPH_ID at 0x%08X reads 0x%08X, expected 0x%08X. "
        "Target %r declares the mailbox at byte 0x%02X — if that byte is wrong, "
        "every cross-die mailbox CAM rule derived from it will DECERR."
        % (board.name, base + H.IPC_PERIPH_ID, periph_id,
           H.IPC_PERIPH_ID_EXPECT, board.target.name,
           board.target.inbound_byte("ipc_mailbox")))


def test_l1_probe_09_board_read_refuses_out_of_window(each_board):
    """L1-PROBE-09: `Board.read()` applies the target guard — on a real board.

    L0-ADDR-04 proves `Target.to_host()` refuses out-of-window addresses; this
    proves the Board actually routes through it, which is what stops a test from
    ever constructing a raw address. The guard raises before any bus transaction
    is issued, so the call cannot itself wedge anything.
    Pass: AddressGuardError for the first address past the window and for a
    negative address; a `read_many` burst that would walk off the end is refused
    too; the board is still alive afterwards.
    """
    board = each_board
    size = board.target.window_size
    for bad in (size, size + 4, -4):
        with pytest.raises(AddressGuardError):
            board.read(bad)
    with pytest.raises(AddressGuardError):
        board.read_many(size - 4, 4)          # last word walks past the window
    assert board.alive(), (
        "%s stopped answering after a refused out-of-window access — the guard "
        "must reject the address before issuing any transaction" % board.name)
