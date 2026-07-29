"""L4 — two boards, the cross-die DATA PLANE.

    *********************************************************************
    *  ATTENDED ONLY. THESE TESTS CAN WEDGE A BOARD.                    *
    *                                                                   *
    *  The shipped bitstream carries the UPSTREAM, recovery-stripped    *
    *  FCSM on the five AXI data-plane FC nodes (AW/W/B/AR/R); only the *
    *  TideLink sideband node keeps the recovery logic. One bit error,  *
    *  one dropped ACK, one pktnum gap therefore has NO recovery path:  *
    *  the node stops emitting, the far side's B (write) or R (read)    *
    *  beat never returns, the PS M_AXI_GP0 SmartConnect saturates and  *
    *  the whole PL slave set wedges with no software timeout. Recovery *
    *  is a JTAG POR.                                                   *
    *                                                                   *
    *  Calibration is one-shot (`calibrated_once_q` permanently gates   *
    *  off re-trigger), so the sampling point is frozen at bring-up:    *
    *  the FIRST transfer after bring-up reliably passes and each later *
    *  one is likelier to sample a bit wrong. A clean run is a clean    *
    *  BER window, NOT evidence of recovery.                            *
    *                                                                   *
    *  -> docs/CROSS_DIE_WEDGE_ROOTCAUSE.md                             *
    *  -> deselected unless --data-plane; never run these in CI.        *
    *********************************************************************

The verdict everywhere below is a **LOCAL read on the receiver**: the receiving
die is poisoned first, the payload is written across the link, then the receiver
reads it out of its own SRAM — no link traversal in the check itself. That is the
wedge-safe way to prove a write crossed, and it is why the peer READ round-trip
is a separate, opt-in test (L4-DATA-06).

Copyright (C) 2026, SoC Labs (www.soclabs.org)
"""
from __future__ import annotations

import pytest

from hetsoc import regs

import _helpers as H

pytestmark = [pytest.mark.l4, pytest.mark.hardware, pytest.mark.pair]

PAYLOAD_FWD = 0xC0FFEE01      # the value proven on silicon, A -> B
PAYLOAD_REV = 0xB2A0FEED      # distinct, B -> A
PAYLOAD_MBOX = 0x5EED0001
CONTROL_PAYLOAD = 0x0BADC0DE

#: Candidate "excluded" bytes for the confinement test — regions the far die's
#: D2D initiator must NOT be able to reach. Filtered at runtime against the real
#: descriptors so this stays correct on a heterogeneous pair.
EXCLUDED_BYTE_CANDIDATES = (0x2C, 0x28, 0x21, 0x24, 0x29)


@pytest.fixture(autouse=True)
def cam_restore(linked_pair):
    """Leave both translators disarmed after every data-plane test.

    The CAM resets on `hresetn`, not on POR, so an armed rule outlives the test
    that set it. Disarming keeps the module order-independent, and means a later
    stray write into a peer aperture carries an untranslated address (which
    DECERRs at the far die) rather than a live retarget.
    """
    yield
    for board in linked_pair.boards:
        board.reg_write(regs.CAM_CTRL, 0)


def _poison(dst, which, offset, value=H.POISON):
    """Write a known non-payload value where a cross-die write should land.

    Without this a stale value from an earlier run would pass the test with the
    ribbon unplugged. Zero would be useless as a poison: 0x00000000 is the
    signature of the peer-write data-phase drop the two-SoC sim found, so a test
    must be able to tell "never written" from "written as zero".
    """
    address = dst.target.inbound_soc_base(which) + offset
    dst.write(address, value)
    assert dst.read(address) == value, (
        "%s: could not poison its own %s at 0x%08X before the transfer. The "
        "receiver cannot see its own memory, so nothing this test reports about "
        "the link would mean anything." % (dst.name, which, address))
    return address


def _await_landing(pair, dst, which, offset, payload, poison=H.POISON):
    """Poll the receiver's LOCAL memory until the payload lands.

    Deterministic — a deadline, and a message that distinguishes the three
    failure modes (nothing crossed / the address crossed but the data did not /
    something else entirely).
    """
    try:
        return H.poll_until(
            lambda: pair.read_landed(dst, which, offset),
            lambda v: v == payload, timeout_s=2.0,
            what="%s local %s at offset 0x%X" % (dst.name, which, offset))
    except H.PollTimeout:
        got = pair.read_landed(dst, which, offset)
        if got == poison:
            pytest.fail(
                "%s: the landing address still holds the poison 0x%08X — NOTHING "
                "crossed the link. Check the CAM rule is armed, the link is still "
                "at FCSM=%d, and that the sender's peer aperture byte (0x%02X) is "
                "the one chiplet_d2d_decode selects."
                % (dst.name, poison, regs.FCSM_LINK_IDLE,
                   pair.other(dst).target.peer_aperture), pytrace=False)
        if got == 0:
            pytest.fail(
                "%s: the landing address reads 0x00000000 — the ADDRESS crossed "
                "but the DATA did not. This is the peer-write data-phase drop the "
                "two-SoC sim found: the payload leaves the sender on d2d_ahb_m and "
                "arrives as zero at the receiver's inbound port." % dst.name,
                pytrace=False)
        pytest.fail("%s: landing address = 0x%08X, expected the payload 0x%08X"
                    % (dst.name, got, payload), pytrace=False)


# ===========================================================================
# Harness proof — no link involved
# ===========================================================================

def test_l4_data_01_each_die_reaches_its_own_shared_sram(linked_pair):
    """L4-DATA-01: each die can write and read back its OWN shared_sram_0.

    Deliberately NOT marked `data_plane`: nothing crosses the link, so this is
    wedge-free and runs by default. It exists to make every other verdict in this
    module interpretable — when a cross-die transfer fails, this says immediately
    whether the receiver could see its own memory at all. It is the on-silicon
    twin of the two-SoC sim's `test_smoke_eth_ss0_reaches_sram`.
    Pass: both dies write and read back distinct values at distinct offsets in
    their own shared SRAM.
    """
    assert H.XFER_OFFSET + 4 * H.XFER_WINDOW_WORDS <= H.SHARED_SRAM_SIZE, (
        "the transfer window would run past the %d-byte shared_sram_0"
        % H.SHARED_SRAM_SIZE)
    for board, value in ((linked_pair.a, 0xA5A50001), (linked_pair.b, 0x5A5A0002)):
        address = board.target.inbound_soc_base("shared_sram") + H.XFER_OFFSET
        board.write(address, value)
        got = board.read(address)
        assert got == value, (
            "%s: local shared_sram_0[0x%08X] = 0x%08X after writing 0x%08X. The "
            "PS -> SoC matrix -> SRAM path is broken on this die; no cross-die "
            "result from this session is meaningful."
            % (board.name, address, got, value))


# ===========================================================================
# Cross-die transfers
# ===========================================================================

@pytest.mark.data_plane
def test_l4_data_02_forward_transfer_lands_in_far_sram(linked_pair,
                                                       link_health_guard):
    """L4-DATA-02: a peer write on die A lands in die B's shared_sram_0.

    The core cross-die proof, and the one already demonstrated on silicon
    (milestone M1). The path is die A's backdoor -> SoC matrix -> d2d_ahb_m ->
    chiplet_d2d_decode (hsel_peer) -> die A's tidelink ahb_sub -> the CAM rewrites
    addr[31:24] -> PHY -> die B's ahb_mng -> die B's SoC matrix -> die B's
    shared_sram_0.

    The verdict is die B reading its OWN SRAM, which is what makes it wedge-safe.
    The receiver is poisoned first so a stale value cannot fake a pass.
    Pass: die B's local shared_sram_0 at the transfer offset reads exactly the
    payload, and the link is still up afterwards.
    """
    a, b = linked_pair.a, linked_pair.b
    _poison(b, "shared_sram", H.XFER_OFFSET)
    linked_pair.cross_die_write(a, "shared_sram", H.XFER_OFFSET, PAYLOAD_FWD)
    got = _await_landing(linked_pair, b, "shared_sram", H.XFER_OFFSET,
                         PAYLOAD_FWD)
    assert got == PAYLOAD_FWD


@pytest.mark.data_plane
def test_l4_data_03_reverse_transfer_lands_in_near_sram(linked_pair,
                                                        link_health_guard):
    """L4-DATA-03 (backlog #2): a peer write on die B lands in die A's
    shared_sram_0 — the SLAVE-to-MASTER direction.

    Everything proven on silicon so far is master->slave. TideLink's own harness
    flagged slave->master as the harder direction, so this closes a named unknown
    rather than repeating a known result.

    The flow is board-symmetric because the CAM rule is derived per direction:
    the match byte from the sender's aperture, the replace byte from the
    receiver's inbound map. That is also exactly what makes it correct on a
    heterogeneous pair, where the two are different designs.
    Pass: die A's local shared_sram_0 reads the reverse payload; link still up.
    """
    a, b = linked_pair.a, linked_pair.b
    _poison(a, "shared_sram", H.XFER_OFFSET)
    linked_pair.cross_die_write(b, "shared_sram", H.XFER_OFFSET, PAYLOAD_REV)
    got = _await_landing(linked_pair, a, "shared_sram", H.XFER_OFFSET,
                         PAYLOAD_REV)
    assert got == PAYLOAD_REV


@pytest.mark.data_plane
def test_l4_data_04_multi_word_sequence_is_not_corrupted(linked_pair,
                                                         link_health_guard):
    """L4-DATA-04: eight consecutive peer writes all land, each with its own
    value.

    Proves what a single beat cannot: an off-by-one *between* beats. A write-data
    delay that misaligned beat N with beat N+1 would corrupt a sequence while a
    lone access passed — the exact class of bug the two-SoC sim added its
    Stage 2c multi-word sequence to catch. Distinct per-word values make a
    cross-beat swap visible instead of self-cancelling.
    Pass: all eight receiver-local words hold their own distinct payload, in
    order, with no duplication.
    """
    a, b = linked_pair.a, linked_pair.b
    count = 8
    values = [0x5EED0000 + (i << 4) + i for i in range(count)]
    base = H.XFER_OFFSET
    assert base + 4 * count <= H.SHARED_SRAM_SIZE

    for i in range(count):
        _poison(b, "shared_sram", base + 4 * i, H.POISON + i)
    linked_pair.map_peer_to(a, "shared_sram")
    for i, value in enumerate(values):
        linked_pair.peer_write(a, a.target.peer(base + 4 * i), value)

    _await_landing(linked_pair, b, "shared_sram", base + 4 * (count - 1),
                   values[-1], H.POISON + count - 1)
    got = b.read_many(b.target.inbound_soc_base("shared_sram") + base, count)
    assert got == values, (
        "%s: the sequenced peer writes landed as %s, expected %s. A word holding "
        "its neighbour's value is a cross-beat write-data skew; 0x%08X+i is the "
        "untouched poison; zero is the data-phase drop."
        % (b.name, ["0x%08X" % v for v in got],
           ["0x%08X" % v for v in values], H.POISON))


@pytest.mark.data_plane
def test_l4_data_05_cam_off_leaves_the_far_sram_untouched(linked_pair,
                                                          link_health_guard):
    """L4-DATA-05: with the CAM disabled, a peer write does NOT reach the far
    die's shared_sram_0.

    The control for L4-DATA-02. Without it, "the payload appeared at 0x2D......"
    proves only that *something* wrote there — not that the CAM's byte
    replacement is what moved it. With `global_enable=0` the translator is an
    identity map, so the far die sees the sender's raw aperture byte, which is
    not in its D2D initiator's target list and DECERRs at the default slave.
    Pass: the receiver's shared_sram_0 still holds its poison, the write returned
    without hanging, and both boards are still alive.
    """
    a, b = linked_pair.a, linked_pair.b
    landed = _poison(b, "shared_sram", H.XFER_OFFSET)

    linked_pair.map_peer_to(a, "shared_sram", enable=False)
    linked_pair.peer_write(a, a.target.peer(H.XFER_OFFSET), CONTROL_PAYLOAD)

    got = b.read(landed)
    assert got == H.POISON, (
        "%s: shared_sram_0[0x%08X] changed to 0x%08X with the CAM DISABLED. The "
        "payload reached the far SRAM without a byte replacement, so "
        "L4-DATA-02's landing does not prove the translation did anything — the "
        "address must be arriving as 0x%02X.... by some other route."
        % (b.name, landed, got, b.target.inbound_byte("shared_sram")))
    assert all(linked_pair.alive().values()), (
        "a peer write through a disabled CAM should DECERR at the far die's "
        "default slave and return cleanly; instead a board stopped answering: %s"
        % linked_pair.alive())


@pytest.mark.data_plane
@pytest.mark.peer_read
def test_l4_data_06_peer_read_round_trip(linked_pair, allow_peer_read,
                                         link_health_guard):
    """L4-DATA-06: die A reads its peer aperture back OVER the link.

    ** THE MOST WEDGE-PRONE ACCESS IN THE SUITE. ** Opt-in behind
    `--allow-peer-read`. An intermittent read-return hang wedged BOTH boards on
    2026-07-29: the read-completion guard (`rd_pipe_r`) exists only in a local RTL
    override, and the silicon build resolves `tidelink_top` via the base copy, so
    reads are at least as fragile as writes.

    Worth having anyway: it is the only test of the request-out/data-back path
    through two real SoCs, and the write direction is already proven wedge-safely
    by L4-DATA-02's local read.
    Pass: the peer read returns the payload the preceding write put there.
    `peer_read()` calls `require_link_up()` first — a peer access on a down link
    hangs the PS bus.
    """
    a, b = linked_pair.a, linked_pair.b
    _poison(b, "shared_sram", H.XFER_OFFSET)
    linked_pair.cross_die_write(a, "shared_sram", H.XFER_OFFSET, PAYLOAD_FWD)
    _await_landing(linked_pair, b, "shared_sram", H.XFER_OFFSET, PAYLOAD_FWD)

    peer = a.target.peer(H.XFER_OFFSET)
    got = H.call_guarded(20.0, linked_pair.peer_read, a, peer)

    assert got == PAYLOAD_FWD, (
        "%s: the peer read of 0x%08X returned 0x%08X, expected 0x%08X. The write "
        "reached %s (verified by its own local read), so the request-out or the "
        "data-back half of the round trip lost it."
        % (a.name, peer, got, PAYLOAD_FWD, b.name))


# ===========================================================================
# The second inbound target: the IPC mailbox
# ===========================================================================

@pytest.fixture
def mailbox_message(linked_pair):
    """Send a 4-word message + MSG_VALID from die A to die B's ipc_mailbox_0.

    A fixture rather than a test step, so the two mailbox assertions (payload and
    interrupt source) are independent tests over one message instead of one test
    with two reasons to fail. It clears the receiver's W1C IRQ_STATUS first, so
    the latch L4-DATA-08 observes provably comes from *this* message's MSG_VALID
    edge and not from an earlier run.
    """
    a, b = linked_pair.a, linked_pair.b
    mbox = b.target.inbound_soc_base("ipc_mailbox")
    words = [(PAYLOAD_MBOX + i) & 0xFFFFFFFF for i in range(regs.IPC_SLOT0_NWORDS)]

    for i in range(regs.IPC_SLOT0_NWORDS):
        b.write(mbox + regs.IPC_SLOT0_DATA + 4 * i, H.POISON + i)
    b.write(mbox + regs.IPC_SLOT0_CTRL, 0)
    b.write(mbox + regs.IPC_IRQ_STATUS, regs.IPC_MSG_VALID)      # W1C
    assert b.read(mbox + regs.IPC_IRQ_STATUS) & regs.IPC_MSG_VALID == 0, (
        "%s: ipc_mailbox_0 IRQ_STATUS[0] would not clear (W1C), so the "
        "interrupt-source assertion could not tell this message from an older "
        "one" % b.name)

    # One CAM byte is the whole difference from the SRAM flow: the aperture is
    # retargeted from the far die's shared_sram byte to its ipc_mailbox byte.
    linked_pair.mailbox_send(a, words)
    return {"words": words, "sender": a, "receiver": b}


@pytest.mark.data_plane
def test_l4_data_07_mailbox_message_crosses(linked_pair, mailbox_message,
                                            link_health_guard):
    """L4-DATA-07 (backlog #1): a doorbell message crosses to the far die's
    ipc_mailbox_0.

    Exercises the **second** inbound D2D target — the one silicon has never
    touched. Inbound from the far die reaches exactly two regions, `shared_sram_0`
    and `ipc_mailbox_0`; the SRAM half is proven, this is the other half, and
    together they complete the data-plane + control-plane IPC story. It costs one
    CAM byte on the already-proven peer-write path.

    The heterogeneous subtlety is that the replace byte comes from the RECEIVER:
    eth->eth is 0x2F->0x23, but eth->compute is 0x2F->0x2A, because the compute
    chiplet's mailbox is at 0x2A (its 0x22-0x23 is the Cortex-M4 SRAM bit-band
    alias and is deliberately unmapped).
    Pass: the receiver's LOCAL mailbox reads MSG_VALID=1 and the exact four words
    the sender wrote.
    """
    b = mailbox_message["receiver"]
    words = mailbox_message["words"]

    H.poll_until(lambda: linked_pair.mailbox_recv(b)["msg_valid"],
                 lambda v: v == 1, timeout_s=2.0,
                 what="%s ipc_mailbox_0 SLOT0_CTRL.MSG_VALID" % b.name)
    received = linked_pair.mailbox_recv(b)

    assert received["words"] == words, (
        "%s: mailbox slot 0 holds %s, expected %s. MSG_VALID arrived, so the "
        "control write crossed — if the data words are the 0x%08X+i poison the "
        "payload beats did not, and if they are zero this is the peer-write "
        "data-phase drop."
        % (b.name, ["0x%08X" % v for v in received["words"]],
           ["0x%08X" % v for v in words], H.POISON))
    assert received["msg_valid"] == 1


@pytest.mark.data_plane
def test_l4_data_08_mailbox_latches_the_cross_die_interrupt_source(
        linked_pair, mailbox_message, link_health_guard):
    """L4-DATA-08: the far-die write latches the near-die mailbox interrupt
    SOURCE.

    Proves cross-die interrupt *generation* with no firmware at all.
    `ipc_mailbox_0` latches IRQ_STATUS[0] on the SLOT0 MSG_VALID edge regardless
    of `irq_enable`, and that bit feeds CPU1's NVIC IRQ0 with no mask in between —
    so a far-die write demonstrably raises the near-die interrupt source on
    silicon. Actual ISR *delivery* needs firmware (both cores are boot-gated in
    the PS flow) and is L6-FUTURE-03.

    The fixture cleared this W1C bit before sending, so a set bit can only have
    come from this message.
    Pass: `irq_latched` is 1 on the receiver.
    """
    b = mailbox_message["receiver"]
    H.poll_until(lambda: linked_pair.mailbox_recv(b)["irq_latched"],
                 lambda v: v == 1, timeout_s=2.0,
                 what="%s ipc_mailbox_0 IRQ_STATUS[0] (the slot0 msg edge)"
                      % b.name)
    received = linked_pair.mailbox_recv(b)
    assert received["irq_latched"] == 1, (
        "%s: IRQ_STATUS=0x%08X — the mailbox message arrived but the MSG_VALID "
        "edge did not latch the interrupt source that feeds CPU1 NVIC IRQ0"
        % (b.name, received["irq_status"]))


# ===========================================================================
# Negative: inbound confinement
# ===========================================================================

@pytest.mark.data_plane
def test_l4_data_09_inbound_is_confined_to_the_permitted_regions(
        linked_pair, link_health_guard):
    """L4-DATA-09 (backlog #6): a peer write retargeted to an EXCLUDED region is
    rejected cleanly and never lands.

    ** HIGHEST WEDGE RISK IN THE MODULE — stage a JTAG POR before running it. **

    Proves the far die's D2D initiator really is confined to its two permitted
    inbound regions and — just as important — that the resulting DECERR comes
    back as an error rather than a hang. A confinement violation that *hangs* is
    far worse than one that lets the write through: it takes the board with it.

    The rule is armed through `allow_unmapped=True`, the deliberate escape hatch
    from the guard L2-CFG-09 verifies; that is the only place in the suite that
    uses it.
    Pass: the write returns (no hang), the receiver's shared_sram_0 still holds
    its poison, both boards are alive, the link is still at FCSM=4, and no new
    sticky fault latched.
    """
    a, b = linked_pair.a, linked_pair.b
    forbidden = [byte for byte in EXCLUDED_BYTE_CANDIDATES
                 if byte not in b.target.inbound_targets.values()
                 and byte not in (a.target.peer_aperture, b.target.peer_aperture)]
    if not forbidden:
        pytest.skip("no excluded byte is available for target %r that is not one "
                    "of its inbound regions or a peer aperture" % b.target.name)
    excluded = forbidden[0]

    landed = _poison(b, "shared_sram", H.XFER_OFFSET)
    before = b.health()

    linked_pair.program_cam(a, a.target.peer_aperture, excluded,
                            allow_unmapped=True)
    linked_pair.peer_write(a, a.target.peer(H.XFER_OFFSET), 0xBAD1BAD1)

    alive = linked_pair.alive()
    assert all(alive.values()), (
        "a peer write retargeted to the excluded byte 0x%02X stopped a board "
        "answering (%s). The far die's default slave must terminate an "
        "unpermitted inbound transfer with DECERR, not leave the response beat "
        "outstanding." % (excluded, alive))

    got = b.read(landed)
    assert got == H.POISON, (
        "%s: shared_sram_0[0x%08X] changed to 0x%08X while the CAM pointed at "
        "the EXCLUDED byte 0x%02X. Inbound confinement is not holding — the "
        "write reached a permitted region it was not addressed to."
        % (b.name, landed, got, excluded))

    after = b.health()
    assert after["link_up"], (
        "%s: the link dropped after a DECERRed inbound write: %s"
        % (b.name, H.fmt_health(after)))
    assert after["sticky"] == before["sticky"], (
        "%s: a rejected inbound write latched new sticky faults (0x%X -> 0x%X). "
        "A confinement DECERR is an expected, benign outcome and must not leave "
        "the link faulted." % (b.name, before["sticky"], after["sticky"]))
