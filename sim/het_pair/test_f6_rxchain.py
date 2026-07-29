"""F6 DIAGNOSTIC 2 — walk the RX chain to find where the peer's CR packets die.

Copyright 2026, SoC Labs (www.soclabs.org)

`test_f6_diag` established the stuck term precisely: on BOTH dies the FCSM's
`auto_rx_in_valid` NEVER asserts, while `auto_tx_out_advance` pulses and
`socl_l6_cr_emit_count` saturates at 0xff — i.e. each die is happily EMITTING
CR(0x44) packets and neither die ever RECEIVES one. The pads are toggling.

This module walks the receive chain to localise the break:

    pad_rx / pad_clk_rx
      -> phy (WlinkGPIOPHY)         phy_link_rx_rx_link_data[127:0], lane_mask
      -> llrx (WlinkRxLinkLayer)    io_enable, io_obs_state (byte-align FSM;
                                    2 == error), io_obs_is_short_pkt,
                                    io_obs_is_long_pkt, io_obs_valid,
                                    ecc_corrected/corrupted, sync_detected
      -> rxrouter                   rxrouter_auto_in_valid / out_7_valid
      -> tl2wl.wlink_tidelinktl     auto_rx_in_valid          <-- never 1

The discriminator:
  * link_data stuck at 0 / constant     -> nothing arriving from the PHY
  * link_data churning, obs_valid 0     -> framer never byte-aligns (or ECC
                                           rejects every candidate header)
  * obs_valid pulses, rxrouter out_7 0  -> the packet is routed elsewhere
                                           (wrong data_id -> wrong FC node)

RUN
    make -C sim/het_pair sim MODULE=test_f6_rxchain
"""
import os

import cocotb
from cocotb.triggers import ClockCycles
from cocotb.utils import get_sim_time

from test_het_pair import Pair

os.environ.setdefault("COCOTB_RESOLVE_X", "ZEROS")


def _tl(dut, die):
    return dut.u_dieE.u_tidelink if die == "e" else dut.u_dieC.u_tidelink_0


def _wl(dut, die):
    return _tl(dut, die).u_chiplet_controller.u_wlink


def _val(obj, name):
    try:
        h = getattr(obj, name)
    except AttributeError:
        return None
    try:
        v = h.value
    except Exception:
        return None
    return int(v) if v.is_resolvable else None


def _fmt(v, width=1):
    if v is None:
        return "-"
    return f"0x{v:0{width}x}" if width > 1 else str(v)


WLINK_NETS = [
    ("llrx_io_enable", 1),
    ("llrx_io_active_lanes", 2),
    ("llrx_io_lane_mask", 2),
    ("llrx_io_link_data", 32),
    ("llrx_io_obs_state", 1),
    ("llrx_io_obs_is_short_pkt", 1),
    ("llrx_io_obs_is_long_pkt", 1),
    ("llrx_io_obs_valid", 1),
    ("llrx_io_ecc_corrected", 1),
    ("llrx_io_ecc_corrupted", 1),
    ("llrx_io_in_error_state", 1),
    ("llrx_auto_out_valid", 1),
    ("llrx_auto_out_sop", 1),
    ("llrx_auto_out_data_id", 2),
    ("rxrouter_auto_in_valid", 1),
    ("rxrouter_auto_out_7_valid", 1),
    ("phy_link_rx_rx_link_data", 32),
    ("phy_link_rx_rx_lane_mask", 2),
    ("phy_link_tx_tx_en", 1),
    ("phy_link_tx_tx_link_data", 32),
    ("phy_link_tx_tx_lane_mask", 2),
]

LLRX_INTERNALS = [
    ("state", 1),
    ("byte_count", 2),
    ("word_count", 4),
    ("sync_detected", 1),
    ("sync_resync", 1),
    ("is_short_pkt", 1),
    ("is_long_pkt", 1),
    ("io_robust_sync_seen", 1),
]


def _sweep(dut, tag):
    dut._log.info("=" * 84)
    dut._log.info(f"RX-CHAIN [{tag}]  t={get_sim_time('us'):.1f} us")
    dut._log.info(f"{'net':<38}{'die E (eth/master)':>22}{'die C (cmp/slave)':>22}")
    dut._log.info("-" * 84)
    for name, w in WLINK_NETS:
        e = _fmt(_val(_wl(dut, "e"), name), w)
        c = _fmt(_val(_wl(dut, "c"), name), w)
        dut._log.info(f"{name:<38}{e:>22}{c:>22}")
    dut._log.info(f"{'-- llrx internals --':<38}{'':>22}{'':>22}")
    for name, w in LLRX_INTERNALS:
        e = _fmt(_val(_wl(dut, "e").llrx, name), w)
        c = _fmt(_val(_wl(dut, "c").llrx, name), w)
        dut._log.info(f"{'llrx.' + name:<38}{e:>22}{c:>22}")
    dut._log.info("=" * 84)


async def _churn(dut, cycles=3000):
    """Is the received link word actually MOVING? A framer that never aligns and
    a PHY that delivers nothing look identical in a single sample."""
    seen = {"e": set(), "c": set()}
    counts = {}
    for die in ("e", "c"):
        for k in ("obs_valid", "short", "long", "ecc_corr", "ecc_bad",
                  "sync_det", "out_valid", "rx7_valid", "err_state"):
            counts[(die, k)] = 0
    for _ in range(cycles):
        await ClockCycles(dut.sys_fclk, 1)
        for die in ("e", "c"):
            w = _wl(dut, die)
            d = _val(w, "llrx_io_link_data")
            if d is not None:
                seen[die].add(d)
            for k, net in (("obs_valid", "llrx_io_obs_valid"),
                           ("short", "llrx_io_obs_is_short_pkt"),
                           ("long", "llrx_io_obs_is_long_pkt"),
                           ("ecc_corr", "llrx_io_ecc_corrected"),
                           ("ecc_bad", "llrx_io_ecc_corrupted"),
                           ("out_valid", "llrx_auto_out_valid"),
                           ("rx7_valid", "rxrouter_auto_out_7_valid"),
                           ("err_state", "llrx_io_in_error_state")):
                if _val(w, net) == 1:
                    counts[(die, k)] += 1
            if _val(w.llrx, "sync_detected") == 1:
                counts[(die, "sync_det")] += 1

    dut._log.info(f"RX CHURN over {cycles} sys_fclk cycles")
    dut._log.info(f"  distinct llrx_io_link_data values: "
                  f"die E={len(seen['e'])}  die C={len(seen['c'])}")
    for k in ("obs_valid", "short", "long", "ecc_corr", "ecc_bad", "sync_det",
              "out_valid", "rx7_valid", "err_state"):
        dut._log.info(f"  cycles with {k:<10} high:  die E={counts[('e', k)]:6d}"
                      f"   die C={counts[('c', k)]:6d}")
    for die in ("e", "c"):
        sample = sorted(seen[die])[:4]
        dut._log.info(f"  die {die.upper()} sample link_data: "
                      + ", ".join(f"0x{v:032x}" for v in sample))
    return seen, counts


@cocotb.test(timeout_time=60, timeout_unit="ms")
async def test_f6_rx_chain(dut):
    tb = Pair(dut)
    await tb.reset()
    await tb.wait_role_locked()
    dut._log.info(f"role_locked both dies at t={get_sim_time('us'):.1f} us")
    await tb.wait_cal_done()
    dut._log.info(f"cal_done both dies at t={get_sim_time('us'):.1f} us")

    _sweep(dut, "at cal_done")
    await ClockCycles(dut.sys_fclk, 20000)
    _sweep(dut, "cal_done + 20k")
    await _churn(dut, 3000)

    dut._log.info("DIAGNOSTIC COMPLETE (this test always passes; read the log)")
