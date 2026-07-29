"""F6 CLOSING EXPERIMENT — re-enable the link layer AFTER autonomous training.

Copyright 2026, SoC Labs (www.soclabs.org)

`test_f6_longwatch` produced the decisive trace. Autonomous training does NOT
deadlock — it completes, and then leaves the link layer switched off:

    2485.45 us  master autoneg -> ST_TRAIN_POLL_PEER(14);  both FCSMs at 1
    3148.81 us  master autoneg -> ST_TRAIN_EXIT(15)
    3797.85 us  SLAVE  FCSM 1 -> 0
    3800.27 us  master autoneg -> ST_TRAIN_DONE(16), train_ok_w=1, train_fail_w=0
    3800.33 us  MASTER FCSM 1 -> 0
    ... both FCSMs remain at 0 for the next 8.7 ms (watched to 12485 us) ...

`ST_TRAIN_EXIT` I2C-writes the peer's SWI_TRAINING_MODE := 0 and then asserts a
local swreset for T_SWRESET_HOLD cycles before ST_TRAIN_DONE. That swreset is
what drops both FCSMs from 1 to 0. FCSM state 0 only exits on
`en_ff2_tx_demet_io_out`, i.e. on `io_app_enable` — so after training the Wlink
link layer is disabled and nothing re-enables it.

That is precisely the second half of the manual LL bootstrap:

    LL_SWRESET_ON  (0x00027F08)   <- autonomous training does this itself
    LL_SWRESET_OFF (0x00027F00)
    LL_ENABLE      (0x00027F07)   <- nothing does this on the autonomous path

SIM_PLAN F5/§5 says the LL bootstrap is a no-op from cold POR. Measured: TRUE
(sim/g2_homog_probe test_b2 reaches FCSM 4 with it removed). But it is NOT a
no-op after autonomous training, because training has cleared the very bits that
were at their reset values from cold POR. And the reason the existing bench sees
the bootstrap "hang the bus" (F5) is that it issues it at cal_done (~2.5 ms) —
while the negotiation FSM still owns the register file and training is still
1.3 ms from finishing.

So this test does the thing nobody has tried: wait for `train_ok_w`, THEN issue
the link-layer enable.

Two testcases:
  test_f6_ll_enable_after_train_ok  — the eth die only (the only pokeable die).
  test_f6_ll_enable_both_via_reg    — both dies, by depositing the Wlink enable
                                      on the compute die too. If the eth-only
                                      case fails and this one passes, the
                                      compute die needs a bus (SIM_PLAN F2) or
                                      TideLink must re-enable autonomously.

RUN
    make -C sim/het_pair sim MODULE=test_f6_late_ll
"""
import os

import cocotb
from cocotb.triggers import ClockCycles
from cocotb.utils import get_sim_time

from test_het_pair import (LL_ENABLE, LL_SWRESET_OFF, LL_SWRESET_ON, Pair, _i)

os.environ.setdefault("COCOTB_RESOLVE_X", "ZEROS")

APB_WL_LINK_ENABLE_RESET = 0x0208
TRAIN_OK_BUDGET = 400_000          # cycles after cal_done; train_ok seen at ~3.8 ms


def _ctrl(dut, die):
    tl = dut.u_dieE.u_tidelink if die == "e" else dut.u_dieC.u_tidelink_0
    return tl.u_chiplet_controller


def _fcsm(dut, die):
    return _ctrl(dut, die).u_wlink.tl2wl.wlink_tidelinktl


def _report(dut, tag):
    e, c = _fcsm(dut, "e"), _fcsm(dut, "c")
    e_st, c_st = _i(e.state, -1), _i(c.state, -1)
    dut._log.info("=" * 72)
    dut._log.info(f"LATE-LL [{tag}]  t={get_sim_time('us'):.1f} us")
    dut._log.info(f"  FCSM                eth={e_st}  compute={c_st}")
    dut._log.info(f"  io_app_enable       eth={_i(e.io_app_enable, -1)}  "
                  f"compute={_i(c.io_app_enable, -1)}")
    dut._log.info(f"  en_ff2_tx_demet     eth={_i(e.en_ff2_tx_demet_io_out, -1)}  "
                  f"compute={_i(c.en_ff2_tx_demet_io_out, -1)}")
    dut._log.info(f"  cr_pkt_seen_rx      eth={_i(e.cr_pkt_seen_rx, -1)}  "
                  f"compute={_i(c.cr_pkt_seen_rx, -1)}")
    dut._log.info(f"  crack_pkt_seen_rx   eth={_i(e.crack_pkt_seen_rx, -1)}  "
                  f"compute={_i(c.crack_pkt_seen_rx, -1)}")
    dut._log.info(f"  autoneg state_r     eth={_i(_ctrl(dut, 'e').u_autoneg.state_r, -1)}  "
                  f"compute={_i(_ctrl(dut, 'c').u_autoneg.state_r, -1)}")
    # The clock/reset of the tx domain the FCSM and its enable synchroniser live
    # in. io_app_enable=1 with en_ff2_tx_demet=0 means that synchroniser is held
    # in reset (or its clock has stopped) — which is the whole question.
    for n in ("io_tx_reset", "io_rx_reset", "io_app_reset", "reset"):
        dut._log.info(f"  {n:<18}  eth={_i(getattr(e, n, None), -1)}  "
                      f"compute={_i(getattr(c, n, None), -1)}")
    for n in ("swreset_hold_r", "train_ok_r"):
        dut._log.info(f"  u_autoneg.{n:<18} eth="
                      f"{_i(getattr(_ctrl(dut, 'e').u_autoneg, n, None), -1)}  "
                      f"compute={_i(getattr(_ctrl(dut, 'c').u_autoneg, n, None), -1)}")
    dut._log.info("=" * 72)
    return e_st, c_st


async def _bring_up_and_wait_train_ok(dut, tb):
    await tb.reset()
    await tb.wait_role_locked()
    await tb.wait_cal_done()
    dut._log.info(f"cal_done both dies at t={get_sim_time('us'):.1f} us; "
                  f"waiting for train_ok_w on the master")
    for _ in range(TRAIN_OK_BUDGET // 20):
        if _i(_ctrl(dut, "e").train_ok_w, 0) == 1:
            dut._log.info(f"master train_ok_w=1 at t={get_sim_time('us'):.2f} us "
                          f"(autoneg state_r="
                          f"{_i(_ctrl(dut, 'e').u_autoneg.state_r, -1)})")
            return True
        await ClockCycles(dut.sys_fclk, 20)
    dut._log.warning("master train_ok_w never asserted within budget")
    return False


async def _settle(dut, tb, tag, cycles=40000):
    await ClockCycles(dut.sys_fclk, cycles)
    e_st, c_st = _report(dut, f"{tag}: +{cycles} cycles")
    dut._log.info(f"### LATE-LL RESULT [{tag}]: FCSM eth={e_st} compute={c_st}")
    if e_st == 4 and c_st == 4:
        dut._log.info("### -> F6 CLOSED by this route: the autonomous path needs a "
                      "post-training link-layer re-enable.")
    else:
        dut._log.info("### -> still short of 4 by this route.")


@cocotb.test(timeout_time=120, timeout_unit="ms")
async def test_f6_ll_enable_after_train_ok(dut):
    tb = Pair(dut)
    _report(dut, "start")
    ok = await _bring_up_and_wait_train_ok(dut, tb)
    _report(dut, "at train_ok" if ok else "train_ok TIMED OUT")

    dut._log.info("issuing the LL bootstrap on the ETH die, post-training")
    for val in (LL_SWRESET_ON, LL_SWRESET_OFF, LL_ENABLE):
        await tb.e.apb_write(APB_WL_LINK_ENABLE_RESET, val)
        await ClockCycles(dut.sys_fclk, 20)
        dut._log.info(f"  wrote 0x{val:08x} ok at t={get_sim_time('us'):.1f} us")
    await _settle(dut, tb, "eth-only LL enable after train_ok")


@cocotb.test(timeout_time=120, timeout_unit="ms")
async def test_f6_ll_enable_both_via_reg(dut):
    """Same, but the compute die is re-enabled too — by writing its Wlink
    LINK_ENABLE_RESET register hierarchically, because that die exports no bus
    (SIM_PLAN F2). Establishes whether the compute die ALSO needs the poke."""
    tb = Pair(dut)
    ok = await _bring_up_and_wait_train_ok(dut, tb)
    _report(dut, "at train_ok" if ok else "train_ok TIMED OUT")

    for val in (LL_SWRESET_ON, LL_SWRESET_OFF, LL_ENABLE):
        await tb.e.apb_write(APB_WL_LINK_ENABLE_RESET, val)
        await ClockCycles(dut.sys_fclk, 20)
    dut._log.info("eth die LL bootstrap issued; now forcing the compute die's "
                  "FCSM app-enable path via its Wlink swi_enable")
    for name in ("swi_enable", "swi_link_enable", "enable"):
        try:
            getattr(_ctrl(dut, "c").u_wlink, name).value = 1
            dut._log.info(f"  compute: deposited u_wlink.{name} = 1")
            break
        except AttributeError:
            continue
    else:
        dut._log.warning("  compute: no swi_enable-like net found on u_wlink")
    await _settle(dut, tb, "both dies re-enabled after train_ok")


@cocotb.test(timeout_time=120, timeout_unit="ms")
async def test_f6_observe_after_train_ok(dut):
    """Pure observation, no writes. `test_f6_ll_enable_after_train_ok` showed
    io_app_enable=1 but en_ff2_tx_demet=0 at train_ok, and then its first APB
    write hung — so this variant issues NOTHING and just reports the tx-domain
    clock/reset state, which is what decides whether the FCSM CAN leave 0."""
    tb = Pair(dut)
    ok = await _bring_up_and_wait_train_ok(dut, tb)
    _report(dut, "at train_ok" if ok else "train_ok TIMED OUT")
    await ClockCycles(dut.sys_fclk, 40000)
    _report(dut, "train_ok + 40k cycles")
    await ClockCycles(dut.sys_fclk, 200000)
    _report(dut, "train_ok + 240k cycles")
    dut._log.info("DIAGNOSTIC COMPLETE (this test always passes; read the log)")
