"""L0-SIM-15 — the compute peer aperture and the window alias, decoder in path.

Copyright 2026, SoC Labs (www.soclabs.org)

WHAT THIS SETTLES
-----------------
"Is the compute peer aperture 0x40 or 0x41?" has been open across three
documents and two repos. The reason it stayed open is that BOTH compute G2
testbenches drove `ahb_sub_hsel` from `htrans[1]` directly, bypassing the
decoder — so a passing peer-store test said nothing about which address the
decoder actually selects. G4 fixed that bypass, but its regression test resolves
`CS ?= /home/dam1n19/SoCLabs/temp/compute-system`, an absolute scratch path
outside the repo which predates G2 (CHIPLET_ALIGNMENT_AUDIT.md C2). So the claim
is still not checked against the tree that ships.

This drives the REAL `chiplet_d2d_decode` at both window placements in one
simulation and reads its selects. No SoC, no link, no bypass.

Alongside the compute instance sits the ETH one, whose map is proven on
hardware. That makes the result a COMPARISON rather than an assertion against a
remembered number: if the two disagree in shape, the test says which moved.
"""
import cocotb
from cocotb.triggers import ClockCycles, Timer

HTRANS_NONSEQ = 0b10
HTRANS_IDLE = 0b00

# The region map, relative to WINDOW_BASE (chiplet_d2d_decode.sv:49-67):
#   haddr[31:24]==B+1                     -> peer
#   haddr[31:24]==B & haddr[19:16]==0..4  -> tx / fifo / ptp / tlapb / tcapb
#   anything else in the window           -> internal default: two-cycle ERROR
BLOCK_TX, BLOCK_FIFO, BLOCK_PTP, BLOCK_TLAPB, BLOCK_TCAPB = 0, 1, 2, 3, 4


def _sel(dut, prefix):
    """The six selects of one instance, as a dict."""
    return {n: int(getattr(dut, "%s_hsel_%s" % (prefix, n)).value)
            for n in ("tx", "fifo", "ptp", "tlapb", "tcapb", "peer")}


async def _drive(dut, addr):
    """Present an address phase and let the combinational decode settle."""
    dut.haddr.value = addr
    dut.htrans.value = HTRANS_NONSEQ
    await Timer(1, units="ns")


async def _reset(dut):
    cocotb.start_soon(_clock(dut))
    dut.htrans.value = HTRANS_IDLE
    dut.link_active_i.value = 1
    await ClockCycles(dut.hclk, 6)


async def _clock(dut):
    from cocotb.clock import Clock
    await Clock(dut.hclk, 10, units="ns").start()


@cocotb.test()
async def test_l0_sim_15_compute_peer_aperture_is_0x41(dut):
    """L0-SIM-15: the compute die's peer aperture decodes at 0x41, not 0x40.

    ★ The load-bearing one. 0x40 is the CONFIG half — its block 0 is the TX
    aperture, which TideLink marks a wedge hazard. A sender aiming a peer write
    at 0x4000_xxxx does not reach the far die; it hits its own TX path.
    """
    await _reset(dut)

    await _drive(dut, 0x4100_1000)
    s = _sel(dut, "c")
    assert s["peer"] == 1, ("compute 0x41 did not select the peer aperture: %s. "
                            "If this fails the whole het address map is wrong." % s)
    assert sum(s.values()) == 1, "0x41 selected more than one slave: %s" % s

    # ...and 0x40 must NOT be the peer. It is the config half.
    await _drive(dut, 0x4000_1000)
    s = _sel(dut, "c")
    assert s["peer"] == 0, (
        "compute 0x4000_1000 selected the PEER aperture. That is the 0x40-vs-0x41 "
        "confusion, and it is the dangerous direction: firmware aiming here "
        "would silently drive the TX aperture instead of the link.")
    assert s["tx"] == 1, "0x4000_1000 (block 0) should select the TX aperture: %s" % s

    dut._log.info("compute peer aperture confirmed at 0x41; 0x40 is config/TX")


@cocotb.test()
async def test_l0_sim_15b_eth_and_compute_have_the_same_shape(dut):
    """L0-SIM-15b: both placements decode identically relative to their base.

    The eth map is hardware-proven. Comparing shapes turns 'is 0x41 right?' into
    'does the parameter move the whole map coherently?', which is the property
    G4 actually claims.
    """
    await _reset(dut)
    for block, name in ((BLOCK_TX, "tx"), (BLOCK_FIFO, "fifo"),
                        (BLOCK_PTP, "ptp"), (BLOCK_TLAPB, "tlapb"),
                        (BLOCK_TCAPB, "tcapb")):
        await _drive(dut, 0x2E00_0000 | (block << 16) | 0x40)
        eth = _sel(dut, "e")
        await _drive(dut, 0x4000_0000 | (block << 16) | 0x40)
        cmp_ = _sel(dut, "c")
        assert eth == cmp_, ("block %d (%s) decodes differently: eth=%s compute=%s"
                             % (block, name, eth, cmp_))
        assert eth[name] == 1, "block %d should select %s, got %s" % (block, name, eth)

    # peer halves
    await _drive(dut, 0x2F00_1000)
    assert _sel(dut, "e")["peer"] == 1, "eth 0x2F is the peer half"
    dut._log.info("eth and compute decode the same shape about their own bases")


@cocotb.test()
async def test_l0_sim_15c_the_256mb_window_does_not_alias(dut):
    """L0-SIM-15c: 0x42..0x4F must fault, not alias onto the apertures.

    The compute D2D window is 256 MB but only its first two 16 MB slices are
    mapped. The pre-G4 decoder split on haddr[24] alone, so the upper 224 MB
    ALIASED 8x onto them — a stray pointer anywhere in that range would have
    silently become a peer write. This is the regression test for that.
    """
    await _reset(dut)
    aliased = []
    for top in range(0x42, 0x50):
        await _drive(dut, (top << 24) | 0x0010_0000)
        s = _sel(dut, "c")
        if any(s.values()):
            aliased.append((top, dict(s)))
    assert not aliased, (
        "the upper compute window ALIASES onto real apertures at top byte(s) %s. "
        "A stray access anywhere in 224 MB becomes a peer or config transaction."
        % ", ".join("0x%02X->%s" % (t, [k for k, v in s.items() if v])
                    for t, s in aliased))
    dut._log.info("0x42..0x4F: no aliasing, %d top bytes checked" % (0x50 - 0x42))


@cocotb.test()
async def test_l0_sim_15d_link1_window_is_disjoint_from_link0(dut):
    """L0-SIM-15d: the second D2D window must not answer for the first.

    The compute die has TWO TideLinks (0x4000_0000 and 0x6000_0000). Only link 0
    is cabled on this bench. If link 1's decoder also claimed 0x41, a peer write
    would fan out to an uncabled ribbon.
    """
    await _reset(dut)

    await _drive(dut, 0x4100_1000)
    assert int(dut.l1_hsel_peer.value) == 0, (
        "link 1 selected the peer aperture for a LINK 0 address (0x4100_1000) — "
        "the two windows overlap and a peer write would drive an uncabled ribbon")

    await _drive(dut, 0x6100_1000)
    assert int(dut.l1_hsel_peer.value) == 1, "link 1's own peer half is 0x61"
    assert int(dut.c_hsel_peer.value) == 0, (
        "link 0 answered for a LINK 1 address (0x6100_1000)")
    dut._log.info("link 0 (0x40/0x41) and link 1 (0x60/0x61) are disjoint")
