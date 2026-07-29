"""F6 CONFIRMING EXPERIMENT — disarm `train_auto_en` and see if the link comes up.

Copyright 2026, SoC Labs (www.soclabs.org)

HYPOTHESIS (from test_f6_diag + test_f6_rxchain + the homogeneous-pair controls
in sim/g2_homog_probe): the FCSM stall is caused by TideLink's autonomous
TRAINING rendezvous, not by heterogeneity and not by the chiplet integration.

    tidelink_autoneg.sv, negotiation LOSER:
        ST_NEGO_WAIT(2) --nego_lost--> ST_NEGO_DONE(5)   // "Terminal state"
    tidelink_autoneg.sv, negotiation WINNER, with train_auto_en=1:
        ... -> ST_NEGO_DONE_PRE(11) -> ST_TRAIN_ENTER(12)
            -> ST_TRAIN_RUN(13) -> ST_TRAIN_POLL_PEER(14)  // parks forever

The winner polls a peer that, having taken the terminal loser path, never enters
training. While the winner sits in ST_TRAIN_* the GPIO PHY keeps emitting the
training pattern, so both dies' `WlinkRxLinkLayer.io_link_data` is pinned at
0xED1412EB x4 and no packet is ever framed.

`train_auto_en` is `NEGO_TRAIN_CFG[0]` (APB 0x210C, POR 16'h0001). TideLink's own
axi_chiplet_controller.sv:1278-1281 records that the PROVEN on-silicon manual
recipe "disarms autonomy by writing NEGO_TRAIN_CFG 0x210C = 0 FIRST
(td_v2_hwlib.sh rcp :91)" and calls 0x210C=0 "an immediate on-silicon escape
hatch from a stuck force window".

So: clear train_auto_en, leave NEGO_CFG=0x61 armed (the compute die still has to
lock its role from straps alone — it has no bus), and see whether the FCSM
reaches 4.

Two routes, because they test different things:

  test_f6_disarm_via_apb   — writes 0x2E03_210C = 0 on the ETHERNET die only,
                             early, before the negotiation FSM claims the
                             register file. This is a REAL host action and is
                             what a bring-up script would do. It also probes
                             SIM_PLAN F5 (writes hang once the FSM is running).
  test_f6_disarm_via_param — deposits nego_train_cfg_r = 0 on BOTH dies at reset,
                             i.e. exactly what the parameter
                             NEGO_TRAIN_CFG_RESET = 16'h0000 would do. The
                             compute die has no bus, so on silicon this is the
                             ONLY route available to it. Not a stub: it sets a
                             configuration register to a value the RTL already
                             supports as a POR parameter
                             (axi_chiplet_controller.sv:60).

RUN
    make -C sim/het_pair sim MODULE=test_f6_fix
"""
import os

import cocotb
from cocotb.triggers import ClockCycles
from cocotb.utils import get_sim_time

from test_het_pair import Pair, _i

os.environ.setdefault("COCOTB_RESOLVE_X", "ZEROS")

APB_NEGO_TRAIN_CFG = 0x210C          # NEGO_TRAIN_CFG; [0] = train_auto_en


def _fcsm(dut, die):
    tl = dut.u_dieE.u_tidelink if die == "e" else dut.u_dieC.u_tidelink_0
    return tl.u_chiplet_controller.u_wlink.tl2wl.wlink_tidelinktl


def _ctrl(dut, die):
    tl = dut.u_dieE.u_tidelink if die == "e" else dut.u_dieC.u_tidelink_0
    return tl.u_chiplet_controller


def _deposit_train_cfg_zero(dut, log):
    for name, die in (("die E", "e"), ("die C link0", "c")):
        try:
            _ctrl(dut, die).nego_train_cfg_r.value = 0
            log.info(f"{name}: deposited nego_train_cfg_r = 0 (train_auto_en off)")
        except AttributeError:
            log.warning(f"{name}: nego_train_cfg_r not reachable")


def _report(dut, tag):
    e, c = _fcsm(dut, "e"), _fcsm(dut, "c")
    e_st, c_st = _i(e.state, -1), _i(c.state, -1)
    dut._log.info("=" * 74)
    dut._log.info(f"F6-FIX [{tag}]  t={get_sim_time('us'):.1f} us")
    dut._log.info(f"  FCSM state              eth={e_st}   compute={c_st}   (4 == link up)")
    dut._log.info(f"  cr_pkt_seen_rx          eth={_i(e.cr_pkt_seen_rx, -1)}   "
                  f"compute={_i(c.cr_pkt_seen_rx, -1)}")
    dut._log.info(f"  crack_pkt_seen_rx       eth={_i(e.crack_pkt_seen_rx, -1)}   "
                  f"compute={_i(c.crack_pkt_seen_rx, -1)}")
    dut._log.info(f"  auto_rx_in_valid        eth={_i(e.auto_rx_in_valid, -1)}   "
                  f"compute={_i(c.auto_rx_in_valid, -1)}")
    for die, tag2 in (("e", "eth"), ("c", "compute")):
        w = _ctrl(dut, die).u_wlink
        d = _i(w.llrx_io_link_data, -1)
        dut._log.info(f"  {tag2:<7} llrx_io_link_data = "
                      + (f"0x{d:032x}" if d >= 0 else "X"))
        dut._log.info(f"  {tag2:<7} nego_train_cfg_r  = "
                      f"0x{_i(_ctrl(dut, die).nego_train_cfg_r, -1):04x}")
    dut._log.info("=" * 74)
    return e_st, c_st


async def _finish(dut, tb, tag):
    await ClockCycles(dut.sys_fclk, 20000)
    e_st, c_st = _report(dut, f"{tag}: cal_done + 20k")
    dut._log.info(f"### F6-FIX RESULT [{tag}]: FCSM eth={e_st} compute={c_st}")
    if e_st == 4 and c_st == 4:
        dut._log.info("### -> HYPOTHESIS CONFIRMED: the training rendezvous was the blocker.")
    else:
        dut._log.info("### -> hypothesis NOT confirmed by this route; FCSM still short of 4.")


@cocotb.test(timeout_time=90, timeout_unit="ms")
async def test_f6_disarm_via_param(dut):
    """Deposit nego_train_cfg_r = 0 on both dies, mirroring NEGO_TRAIN_CFG_RESET."""
    tb = Pair(dut)

    dut.e_sysresetn.value = 0
    dut.c_sysresetn.value = 0
    dut.e_pad_en.value = 1
    dut.c_pad_en.value = 1
    await ClockCycles(dut.sys_fclk, 20)
    tb._calibrator_sim_bypass()
    dut.e_sysresetn.value = 1
    dut.c_sysresetn.value = 1
    await ClockCycles(dut.sys_fclk, 200)
    tb._calibrator_sim_bypass()
    tb._check_autoneg_armed()
    _deposit_train_cfg_zero(dut, dut._log)
    await ClockCycles(dut.sys_fclk, 4000)
    _deposit_train_cfg_zero(dut, dut._log)   # after each PRMU releases poresetn

    await tb.wait_role_locked()
    dut._log.info(f"role_locked both dies at t={get_sim_time('us'):.1f} us")
    await tb.wait_cal_done()
    dut._log.info(f"cal_done both dies at t={get_sim_time('us'):.1f} us")
    await _finish(dut, tb, "param route (both dies train_auto_en=0)")


@cocotb.test(timeout_time=90, timeout_unit="ms")
async def test_f6_disarm_via_apb(dut):
    """Write NEGO_TRAIN_CFG=0 on the ETHERNET die only, over its real AHB/APB
    path, early. The compute die keeps train_auto_en=1 — it cannot be poked —
    but it is the negotiation LOSER and never enters training anyway, so
    disarming the WINNER should be sufficient."""
    tb = Pair(dut)
    await tb.reset()
    dut._log.info(f"writing NEGO_TRAIN_CFG=0 on die E at t={get_sim_time('us'):.1f} us")
    await tb.e.apb_write(APB_NEGO_TRAIN_CFG, 0)
    got = await tb.e.apb_read(APB_NEGO_TRAIN_CFG)
    dut._log.info(f"die E NEGO_TRAIN_CFG read back 0x{got:08x} (expect bit0 == 0)")

    await tb.wait_role_locked()
    dut._log.info(f"role_locked both dies at t={get_sim_time('us'):.1f} us")
    await tb.wait_cal_done()
    dut._log.info(f"cal_done both dies at t={get_sim_time('us'):.1f} us")
    await _finish(dut, tb, "apb route (eth die train_auto_en=0)")
