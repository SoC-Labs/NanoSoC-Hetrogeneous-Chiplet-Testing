"""F6 DIAGNOSTIC 3 — the training rendezvous: master polls, slave never joins.

Copyright 2026, SoC Labs (www.soclabs.org)

Confirms the identification of the stall with TideLink's own **Bug N2**
(`tidelink/cocotb/tidelink_top_pair/test_14_bug_n2_slave_training_mode_landed.py`),
whose docstring describes exactly this signature:

    "master enters ST_TRAIN_ENTER (state 12) ... FSM advances to ST_TRAIN_RUN
     (state 13). ... Slave still at ST_NEGO_DONE (state 5) with
     swi_training_mode_r=0 throughout. ... slave's lane_checker never sees
     training_mode=1, so lane_locked stays 0, so master's POLL_PEER eventually
     times out"

The master reaches ST_TRAIN_ENTER by I2C-writing `swi_training_mode = 1` into the
PEER's chiplet controller. If that write does not land in the slave's
`swi_training_mode_r`, the slave's lane checker never locks, the master's
ST_TRAIN_POLL_PEER never sees `all_locked & cal_done & !fault`, and the PHY is
held in training forever.

This module samples, on both dies, for the whole bring-up:
  * u_autoneg.state_r          (5 = ST_NEGO_DONE, 12/13/14 = ST_TRAIN_*)
  * swi_training_mode_r        (the register the master's I2C write must set)
  * u_autoneg.peer_lane_locked_r / poll_attempt_r
  * the controller's local lane-lock status

RUN
    make -C sim/het_pair sim MODULE=test_f6_trainrdv
"""
import os

import cocotb
from cocotb.triggers import ClockCycles
from cocotb.utils import get_sim_time

from test_het_pair import Pair, _i

os.environ.setdefault("COCOTB_RESOLVE_X", "ZEROS")

ST = {5: "ST_NEGO_DONE(terminal)", 11: "ST_NEGO_DONE_PRE", 12: "ST_TRAIN_ENTER",
      13: "ST_TRAIN_RUN", 14: "ST_TRAIN_POLL_PEER", 15: "ST_TRAIN_EXIT",
      16: "ST_TRAIN_DONE", 17: "ST_TRAIN_FAIL", 6: "ST_BYPASS"}


def _ctrl(dut, die):
    tl = dut.u_dieE.u_tidelink if die == "e" else dut.u_dieC.u_tidelink_0
    return tl.u_chiplet_controller


CTRL_NETS = ["swi_training_mode_r", "nego_train_cfg_r", "nego_cfg_reg",
             "autonomy_armed", "autonomy_retire_q", "role_locked",
             "train_ok_w", "train_fail_w", "train_in_progress_w",
             "train_peer_nack_w", "train_peer_lane_locked_w",
             "train_peer_lane_fault_w", "train_local_lane_fault_w",
             "swi_calibration_done"]
ANEG_NETS = ["state_r", "poll_attempt_r", "peer_lane_locked_r",
             "train_poll_phase_r", "train_ok_r"]


def _v(obj, name):
    try:
        h = getattr(obj, name)
    except AttributeError:
        return "-"
    try:
        val = h.value
    except Exception:
        return "?"
    return int(val) if val.is_resolvable else "X"


def _sweep(dut, tag):
    dut._log.info("=" * 76)
    dut._log.info(f"TRAIN-RDV [{tag}]  t={get_sim_time('us'):.1f} us")
    dut._log.info(f"{'net':<34}{'die E (master)':>20}{'die C (slave)':>20}")
    dut._log.info("-" * 76)
    for n in CTRL_NETS:
        dut._log.info(f"{n:<34}{str(_v(_ctrl(dut, 'e'), n)):>20}"
                      f"{str(_v(_ctrl(dut, 'c'), n)):>20}")
    for n in ANEG_NETS:
        e = _v(_ctrl(dut, "e").u_autoneg, n)
        c = _v(_ctrl(dut, "c").u_autoneg, n)
        extra_e = f" ({ST[e]})" if n == "state_r" and e in ST else ""
        extra_c = f" ({ST[c]})" if n == "state_r" and c in ST else ""
        dut._log.info(f"{'u_autoneg.' + n:<34}{str(e) + extra_e:>20}"
                      f"{str(c) + extra_c:>20}")
    dut._log.info("=" * 76)


@cocotb.test(timeout_time=60, timeout_unit="ms")
async def test_f6_training_rendezvous(dut):
    tb = Pair(dut)
    await tb.reset()
    await tb.wait_role_locked()
    dut._log.info(f"role_locked both dies at t={get_sim_time('us'):.1f} us")
    _sweep(dut, "role_locked")
    await tb.wait_cal_done()
    dut._log.info(f"cal_done both dies at t={get_sim_time('us'):.1f} us")
    _sweep(dut, "cal_done")

    # Watch for the master entering the training states and the slave (not)
    # following it. 60k cycles is ~1.2 ms, well past the master's ST_TRAIN_*
    # entry measured at ~2.31/2.39 ms absolute.
    last = {"e": None, "c": None}
    last_tm = {"e": None, "c": None}
    for _ in range(60_000):
        await ClockCycles(dut.sys_fclk, 1)
        for die in ("e", "c"):
            s = _v(_ctrl(dut, die).u_autoneg, "state_r")
            if s != last[die]:
                dut._log.info(f"  [{get_sim_time('us'):9.2f} us] die {die.upper()} "
                              f"autoneg state_r {last[die]} -> {s} "
                              f"{ST.get(s, '')}")
                last[die] = s
            tm = _v(_ctrl(dut, die), "swi_training_mode_r")
            if tm != last_tm[die]:
                dut._log.info(f"  [{get_sim_time('us'):9.2f} us] die {die.upper()} "
                              f"swi_training_mode_r {last_tm[die]} -> {tm}")
                last_tm[die] = tm

    _sweep(dut, "cal_done + 60k cycles")
    dut._log.info("DIAGNOSTIC COMPLETE (this test always passes; read the log)")
