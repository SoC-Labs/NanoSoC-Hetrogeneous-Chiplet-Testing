"""F6 DIAGNOSTIC 4 — deadlock, or merely slower than every budget so far?

Copyright 2026, SoC Labs (www.soclabs.org)

`test_f6_trainrdv` changed the picture. The master does NOT park in
ST_TRAIN_POLL_PEER: it polls for 663 us, the peer's lanes DO lock
(train_peer_lane_locked_w = 0xFF), and it advances to ST_TRAIN_EXIT(15) at
3148.81 us — which is AFTER every sample any earlier diagnostic took. It then sat
in ST_TRAIN_EXIT for the remaining 536 us of that run with train_ok_w = 0.

So the question is now sharp and binary: does the autoneg FSM ever reach
ST_TRAIN_DONE(16) and release the PHY, or is ST_TRAIN_EXIT terminal in practice?
Every failing budget so far has been far too short to tell:

    test_het_pair bring_up()      cal_done + 5,000 cycles   (~2585 us)
    test_f6_diag                  cal_done + 40,000 cycles  (~3285 us)
    test_f6_rxchain               cal_done + 20,000 cycles  (~2885 us)
    test_f6_trainrdv              cal_done + 60,000 cycles  (~3685 us)

This watches for 500,000 cycles (~10 ms) after cal_done, logging every autoneg
state change and every FCSM state change on both dies, and exits early the
moment both FCSMs reach 4.

RUN
    make -C sim/het_pair sim MODULE=test_f6_longwatch
"""
import os

import cocotb
from cocotb.triggers import ClockCycles
from cocotb.utils import get_sim_time

from test_het_pair import Pair, _i

os.environ.setdefault("COCOTB_RESOLVE_X", "ZEROS")

ST = {0: "ST_IDLE", 1: "ST_NEGO_INIT", 2: "ST_NEGO_WAIT", 3: "ST_NEGO_CLAIM",
      4: "ST_NEGO_POLL", 5: "ST_NEGO_DONE(terminal)", 6: "ST_BYPASS",
      7: "ST_ERROR", 8: "ST_NEGO_MASK_RES_TX", 9: "ST_NEGO_MASK_RD_ADDR",
      10: "ST_NEGO_MASK_RD_DATA", 11: "ST_NEGO_DONE_PRE", 12: "ST_TRAIN_ENTER",
      13: "ST_TRAIN_RUN", 14: "ST_TRAIN_POLL_PEER", 15: "ST_TRAIN_EXIT",
      16: "ST_TRAIN_DONE", 17: "ST_TRAIN_FAIL", 18: "ST_FIN_RDV", 19: "ST_FIN_GO"}

WATCH_CYCLES = 500_000


def _ctrl(dut, die):
    tl = dut.u_dieE.u_tidelink if die == "e" else dut.u_dieC.u_tidelink_0
    return tl.u_chiplet_controller


def _fcsm(dut, die):
    return _ctrl(dut, die).u_wlink.tl2wl.wlink_tidelinktl


@cocotb.test(timeout_time=120, timeout_unit="ms")
async def test_f6_long_watch(dut):
    tb = Pair(dut)
    await tb.reset()
    await tb.wait_role_locked()
    await tb.wait_cal_done()
    t_cal = get_sim_time("us")
    dut._log.info(f"cal_done both dies at t={t_cal:.1f} us; watching "
                  f"{WATCH_CYCLES} cycles")

    last_a = {"e": None, "c": None}
    last_f = {"e": None, "c": None}
    reached = None

    for i in range(WATCH_CYCLES):
        await ClockCycles(dut.sys_fclk, 1)
        for die in ("e", "c"):
            a = _i(_ctrl(dut, die).u_autoneg.state_r, -1)
            if a != last_a[die]:
                dut._log.info(f"  [{get_sim_time('us'):9.2f} us] die {die.upper()} "
                              f"autoneg {last_a[die]} -> {a} ({ST.get(a, '?')})")
                last_a[die] = a
            f = _i(_fcsm(dut, die).state, -1)
            if f != last_f[die]:
                dut._log.info(f"  [{get_sim_time('us'):9.2f} us] die {die.upper()} "
                              f"FCSM {last_f[die]} -> {f}")
                last_f[die] = f
        if last_f["e"] == 4 and last_f["c"] == 4:
            reached = get_sim_time("us")
            dut._log.info(f"  BOTH FCSMs reached 4 at t={reached:.2f} us "
                          f"({reached - t_cal:.2f} us after cal_done)")
            break

    dut._log.info("=" * 74)
    dut._log.info("### LONG-WATCH RESULT")
    dut._log.info(f"  watched {WATCH_CYCLES} cycles after cal_done "
                  f"(to t={get_sim_time('us'):.1f} us)")
    for die, tag in (("e", "die E (master)"), ("c", "die C (slave)")):
        a = last_a[die]
        dut._log.info(f"  {tag}: autoneg {a} ({ST.get(a, '?')})   "
                      f"FCSM {last_f[die]}   "
                      f"train_ok_w={_i(_ctrl(dut, die).train_ok_w, -1)} "
                      f"train_fail_w={_i(_ctrl(dut, die).train_fail_w, -1)} "
                      f"train_in_progress_w={_i(_ctrl(dut, die).train_in_progress_w, -1)}")
    if reached:
        dut._log.info(f"### -> NOT A DEADLOCK: the link came up at {reached:.2f} us. "
                      f"Every budget used so far was simply too short.")
    else:
        dut._log.info("### -> DEADLOCK CONFIRMED over this horizon: the FCSM never "
                      "reached 4 within 500,000 cycles of cal_done.")
    dut._log.info("=" * 74)
    dut._log.info("DIAGNOSTIC COMPLETE (this test always passes; read the log)")
