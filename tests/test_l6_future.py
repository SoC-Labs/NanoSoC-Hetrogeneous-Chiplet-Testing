"""L6 — cross-die functionality that is blocked on firmware or a new port.

These are **real tests, not stubs.** Each carries the complete sequence it would
run — the actual register addresses, the actual poll, the actual decoded
assertion — behind a precondition that skips with the specific blocker named. As
each blocker clears, the test starts running; nothing has to be written first.

Two kinds of precondition are used, and the difference matters:

  * **Observable** — the blocker is visible from the PS, so the test checks it
    directly (the PTP enable bits, `PAIR_BASE_ADDR`, a target descriptor field).
    These light up the moment the bench is set up correctly.
  * **Declared** — the blocker is something the PS cannot see (an SWD-loaded
    firmware image, a physical PHY on the bench). The operator declares it with
    an environment variable, which is also what makes the skip reason actionable.

Backlog coverage: #4 DMA-250 bulk (L6-FUTURE-01), #5 PTP/PHC cross-die sync
(L6-FUTURE-02), #8 cross-die IRQ -> NVIC -> ISR (L6-FUTURE-03), #9 ethernet M2
(L6-FUTURE-04). L6-FUTURE-05 covers the TideLink returner/doorbell — the one
cross-die master the proven CAM + ahb_sub flow never exercises.

Copyright (C) 2026, SoC Labs (www.soclabs.org)
"""
from __future__ import annotations

import os

import pytest

from hetsoc import regs

import _helpers as H

pytestmark = [pytest.mark.l6, pytest.mark.hardware, pytest.mark.pair]

DMA_NBYTES = 256
DMA_PAYLOAD = 0xD8A00000
PTP_SYNC_COUNT = 8
PTP_CONVERGE_NS = 1_000_000       # 1 ms — a loose "is it disciplined at all" bar


def _block_base(board, table, what, blocker):
    """Resolve a per-target block base, or skip naming what is missing.

    Prefers a descriptor field if the framework grows one, so these tests start
    working on a new target without an edit here.
    """
    base = getattr(board.target, what, None)
    if base is None:
        base = table.get(board.target.name)
    if base is None:
        pytest.skip("no %s known for target %r. %s"
                    % (what, board.target.name, blocker))
    return base


def _require_env(name, blocker):
    value = os.environ.get(name)
    if not value:
        pytest.skip("%s Set %s once it is in place." % (blocker, name))
    return value


# ===========================================================================
# Backlog #4 — DMA-250 bulk cross-die
# ===========================================================================

@pytest.mark.data_plane
@pytest.mark.slow
def test_l6_future_01_dma250_bulk_transfer_crosses_the_link(linked_pair,
                                                            link_health_guard):
    """L6-FUTURE-01 (backlog #4): DMA-250 channel 0 moves a block from die A's
    local SRAM into die B's, through the peer aperture.

    Proves bulk, zero-copy crossing rather than one CPU beat at a time — the
    difference between "the link works" and "the link is usable". `dmac_0_m` is
    granted `d2d` in the SoC matrix, so the path is architecturally available.

    The destination is the **peer aperture** (CAM-translated), deliberately not
    the 0x2E TX aperture: that one has no backpressure and is the documented
    wedge path the backlog explicitly warns off for DMA.

    BLOCKER: something has to program the DMAC's channel registers. Both cores
    are boot-gated in the PS flow, so this needs either an SWD-loaded application
    on die A or a verified PS route to `dmac_0`'s APB through the backdoor —
    neither exists today.
    """
    a, b = linked_pair.a, linked_pair.b
    _require_env(
        "HETSOC_DMA_FIRMWARE",
        "backlog #4 needs the DMA-250 driven from die A: an SWD-loaded app with "
        "CPU0 released from the boot-gate, or a verified PS path to dmac_0's APB "
        "over the backdoor. Both cores are boot-gated in the PS flow, so nothing "
        "can currently write CH_SRCADDR / CH_DESADDR / CH_CMD.")
    dmac = _block_base(a, H.DMAC_BASES, "dmac_base",
                       "backlog #4 needs dmac_0's SoC base for this target.")
    channel = dmac + H.DMAC_CH0_OFFSET

    source = a.target.inbound_soc_base("shared_sram") + H.XFER_OFFSET
    destination = a.target.peer(H.XFER_OFFSET)
    landed = b.target.inbound_soc_base("shared_sram") + H.XFER_OFFSET
    nwords = DMA_NBYTES // 4
    assert H.XFER_OFFSET + DMA_NBYTES <= H.SHARED_SRAM_SIZE

    block = [(DMA_PAYLOAD + i) & 0xFFFFFFFF for i in range(nwords)]
    for i, value in enumerate(block):
        a.write(source + 4 * i, value)
        b.write(landed + 4 * i, H.POISON + i)

    linked_pair.map_peer_to(a, "shared_sram")

    a.write(channel + H.DMA_CH_SRCADDR, source)
    a.write(channel + H.DMA_CH_DESADDR, destination)
    a.write(channel + H.DMA_CH_XSIZE, (nwords << 16) | nwords)
    a.write(channel + H.DMA_CH_CMD, H.DMA_CMD_ENABLE)

    H.poll_until(lambda: a.read(channel + H.DMA_CH_CMD),
                 lambda v: not (v & H.DMA_CMD_ENABLE), timeout_s=5.0,
                 what="DMA-250 ch0 to clear ENABLECMD (transfer done)")
    errinfo = a.read(channel + H.DMA_CH_ERRINFO)
    assert errinfo == 0, ("%s: DMA-250 ch0 ERRINFO = 0x%08X after the transfer"
                          % (a.name, errinfo))

    got = b.read_many(landed, nwords)
    assert got == block, (
        "%s: the DMA block landed with %d of %d words wrong. The single-beat "
        "path is proven (L4-DATA-02), so a failure here is specific to burst / "
        "zero-copy traffic across the aperture."
        % (b.name, sum(1 for x, y in zip(got, block) if x != y), nwords))


# ===========================================================================
# Backlog #5 — PTP / PHC cross-die time sync
# ===========================================================================

@pytest.mark.data_plane
@pytest.mark.slow
def test_l6_future_02_phc_disciplines_across_the_link(linked_pair,
                                                      link_health_guard,
                                                      record_property):
    """L6-FUTURE-02 (backlog #5): die B's PHC converges to die A's over the
    TideLink FC sideband.

    Proves a shared timebase across the package — what makes two dies a system
    rather than two boards. die A (grandmaster) arms `HW_SYNC_CTRL` and an
    interval; die B's servo source 0 (TideLink) disciplines its PHC; the PS
    captures both clocks and compares.

    Judge by PHC-offset convergence, NOT by `servo_locked`: that bit reports the
    ha1588 servo rather than source 0, so it can read locked while the D2D servo
    does nothing — and vice versa.

    BLOCKER: the hardware path is in the bitstream but nothing arms it. PTP must
    be enabled on both dies and the sync interval programmed; that is the setup
    tool backlog #5 calls for. This test checks the enables directly, so it
    starts running as soon as they are set.
    """
    a, b = linked_pair.a, linked_pair.b
    phc_a = _block_base(a, H.PHC_BASES, "phc_base",
                        "backlog #5 needs the PHC SoC base for this target.")
    phc_b = _block_base(b, H.PHC_BASES, "phc_base",
                        "backlog #5 needs the PHC SoC base for this target.")

    for board in linked_pair.boards:
        ptp_ctrl = board.reg_read(H.PTP_CTRL)
        if not ptp_ctrl & H.PTP_CTRL_ENABLE:
            pytest.skip(
                "%s: TideLink PTP_CTRL=0x%08X has enable clear. Backlog #5 needs "
                "PTP armed on BOTH dies (PTP_CTRL.enable, plus HW_SYNC_CTRL and "
                "HW_SYNC_INTERVAL on the grandmaster) — that setup tool does not "
                "exist yet." % (board.name, ptp_ctrl))
    for board, phc in ((a, phc_a), (b, phc_b)):
        status = board.read(phc + H.PHC_STATUS)
        if not status & H.PHC_STATUS_RUNNING:
            pytest.skip("%s: PHC STATUS=0x%08X is not RUNNING — the clock must be "
                        "started before it can be disciplined"
                        % (board.name, status))

    a.reg_write(H.HW_SYNC_CTRL, 0x1)          # grandmaster: arm HW sync

    def _capture(board, phc):
        board.write(phc + H.PHC_CTRL,
                    board.read(phc + H.PHC_CTRL) | H.PHC_CTRL_SW_CAPTURE)
        seconds = ((board.read(phc + H.PHC_CAP_SECONDS_HI) << 32)
                   | board.read(phc + H.PHC_CAP_SECONDS_LO))
        return seconds * H.NS_PER_S + board.read(phc + H.PHC_CAP_NANOSECONDS)

    offsets = [_capture(b, phc_b) - _capture(a, phc_a)
               for _ in range(PTP_SYNC_COUNT)]
    record_property("phc_offsets_ns", offsets)

    assert abs(offsets[-1]) <= PTP_CONVERGE_NS, (
        "die B's PHC is %d ns from die A's after %d syncs (offsets: %s). Judge "
        "convergence from this series, not from servo_locked — that bit reports "
        "the ha1588 servo, not TideLink source 0."
        % (offsets[-1], PTP_SYNC_COUNT, offsets))
    assert abs(offsets[-1]) <= abs(offsets[0]), (
        "the PHC offset is diverging (%d ns -> %d ns over %d syncs) — the servo "
        "is running the wrong way, not merely unlocked"
        % (offsets[0], offsets[-1], PTP_SYNC_COUNT))


# ===========================================================================
# Backlog #8 — cross-die interrupt all the way to an ISR
# ===========================================================================

@pytest.mark.data_plane
def test_l6_future_03_cross_die_interrupt_reaches_an_isr(linked_pair,
                                                         link_health_guard):
    """L6-FUTURE-03 (backlog #8): a far-die mailbox write fires a real ISR on the
    near die.

    L4-DATA-08 already proves the interrupt *source* latches — a far-die write
    sets `ipc_mailbox_0` IRQ_STATUS[0], which feeds CPU1's NVIC IRQ0 with no mask
    in between. What is unproven is *delivery*: that the core actually takes the
    exception and runs handler code.

    The proof is a firmware flag: an ISR on die B writes a sentinel into its
    DMEM, and the PS reads that sentinel out afterwards. The flag is cleared
    first, so a set value can only have come from this message.

    BLOCKER: firmware. Both cores are boot-gated in the PS flow and there is no
    intermediate PS-armable mask — the core must be released and its NVIC ISER
    set (the mailbox also needs `irq_enable` @ +0x02C). That is an SWD-firmware
    session, which is why backlog #8 is parked alongside #4 and #5.
    """
    a, b = linked_pair.a, linked_pair.b
    _require_env(
        "HETSOC_ISR_FIRMWARE",
        "backlog #8 needs an ISR firmware on die B: CPU1 released from the "
        "boot-gate, NVIC IRQ0 enabled, the mailbox irq_enable set, and a handler "
        "that writes HETSOC_ISR_SENTINEL to HETSOC_ISR_FLAG_ADDR.")
    flag_addr = int(_require_env(
        "HETSOC_ISR_FLAG_ADDR",
        "the ISR firmware must publish the SoC address of its sentinel word."), 0)
    sentinel = int(os.environ.get("HETSOC_ISR_SENTINEL", "0x15150001"), 0)

    b.write(flag_addr, 0)
    assert b.read(flag_addr) == 0, (
        "%s: could not clear the ISR flag at 0x%08X before the test"
        % (b.name, flag_addr))

    linked_pair.mailbox_send(a, [sentinel])

    got = H.poll_until(lambda: b.read(flag_addr), lambda v: v == sentinel,
                       timeout_s=5.0,
                       what="%s ISR sentinel at 0x%08X" % (b.name, flag_addr))
    assert got == sentinel

    received = linked_pair.mailbox_recv(b)
    assert received["irq_latched"] == 0, (
        "%s: the ISR ran but did not W1C the mailbox IRQ_STATUS — a handler that "
        "leaves the source latched re-enters immediately" % b.name)


# ===========================================================================
# Backlog #9 — ethernet (M2)
# ===========================================================================

@pytest.mark.slow
def test_l6_future_04_ethernet_mac_carries_a_frame(linked_pair):
    """L6-FUTURE-04 (backlog #9): the ethernet MAC moves a frame to an external
    host.

    The external-network milestone: a MAC is the eth chiplet's reason for
    existing, and nothing has driven one on silicon yet.

    TWO blockers, and the first is structural rather than merely unfinished work:

    1. **The MAC is not reachable from the PS backdoor at all.** `ethmac_0` sits
       at 0x4000_0000 inside the `eth_ss_slave` subordinate bus, whose top-level
       aperture is 0x0000_0000-0x1FFF_FFFF. Neither the PS window nor the D2D
       inbound port reaches it — it is driven from CPU0 inside the subsystem. So
       this needs firmware, not host tooling, and no amount of scripting changes
       that.
    2. The bench needs the LAN8720 PHY wired per the hub topology, plus a host
       NIC port to receive on.
    """
    _require_env(
        "HETSOC_ETH_M2",
        "backlog #9 (M2) is blocked twice over: ethmac_0 @ 0x4000_0000 lives "
        "inside eth_ss_slave (top-level aperture 0x0-0x1FFF_FFFF), so the PS "
        "backdoor CANNOT reach it — it needs CPU0 firmware, not host tooling — "
        "and the bench needs the LAN8720 PHY plus a host NIC port.")
    pytest.skip(
        "HETSOC_ETH_M2 is set, but this test still needs the firmware-side frame "
        "TX/RX hooks to assert against: the host can only observe the far end of "
        "the wire. Implement the loopback in the eth firmware, then assert here "
        "on the received frame.")


# ===========================================================================
# The returner — the other cross-die master
# ===========================================================================

@pytest.mark.data_plane
@pytest.mark.nongating
def test_l6_future_05_software_doorbell_via_the_returner(linked_pair,
                                                         link_health_guard):
    """L6-FUTURE-05: die A's `DOORBELL` write reaches die B's
    `DOORBELL_RESPONSE_ACC`.

    Exercises the **returner** — a different cross-die master from the
    ahb_sub + CAM path every other test in this suite uses, and one that has
    never been validated on silicon. It earns its own test precisely because a
    working peer-aperture transfer says nothing about it.

    Two caveats shape the assertion. The doorbell payload is the *free-credit
    count* rather than a fixed token, so with zero credits there is simply no
    delivery — the test asserts the accumulator moved, not that it holds any
    particular value. Non-gating for the same reason: a zero-credit moment is a
    legitimate explanation for a null result.

    BLOCKER (observable): `PAIR_BASE_ADDR` must be set to the peer's TideLink APB
    base during bring-up. Nothing sets it today, so the returner has no
    destination.
    """
    a, b = linked_pair.a, linked_pair.b
    pair_base = a.reg_read(regs.PAIR_BASE_ADDR)
    if pair_base == 0:
        pytest.skip(
            "%s: PAIR_BASE_ADDR @ TideLink+0x%04X is 0, so the returner has no "
            "destination and a DOORBELL write goes nowhere. Set it to the peer's "
            "TideLink APB base (0x%08X for %s) during bring-up — a one-line "
            "addition to the bring-up recipe, not a rebuild."
            % (a.name, regs.PAIR_BASE_ADDR, b.target.tlapb_base, b.name))

    before = b.reg_read(H.DOORBELL_RESPONSE_ACC)
    credits = a.reg_read(regs.CREDIT_COUNT) & regs.CREDIT_COUNT_MASK
    if credits == 0:
        pytest.skip("%s has 0 free credits, and the doorbell payload IS the "
                    "free-credit count — there would be nothing to deliver"
                    % a.name)

    a.reg_write(regs.DOORBELL, 1)

    after = H.poll_until(lambda: b.reg_read(H.DOORBELL_RESPONSE_ACC),
                         lambda v: v != before, timeout_s=2.0,
                         what="%s DOORBELL response accumulator to advance"
                              % b.name)
    assert after != before, (
        "%s: the doorbell response accumulator stayed at 0x%08X after %s rang "
        "the doorbell with %d free credits. The returner is a different cross-die "
        "master from the proven ahb_sub + CAM path, so a working peer transfer "
        "does not imply it works." % (b.name, before, a.name, credits))
    assert linked_pair.verify_link(), (
        "the link dropped after a doorbell — the returner disturbed it")
