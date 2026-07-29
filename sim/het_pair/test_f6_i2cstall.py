"""F6 DIAGNOSTIC 5 — pin the stall inside ST_TRAIN_EXIT's I2C transaction.

Copyright 2026, SoC Labs (www.soclabs.org)

`test_f6_trainrdv` localised the stall to `tidelink_autoneg.sv` ST_TRAIN_EXIT
(state 15). That state does exactly one thing (tidelink_autoneg.sv, ST_TRAIN_EXIT
body): a 6-byte I2C write of `SWI_TRAINING_MODE := 0` into the PEER's chiplet
controller, then

    TXN_CHECK: if (!axl_rdata_r[I2C_STS_BUSY] && busy_seen_r) begin
                 ... train_ok_nxt = 1'b1; state_nxt = ST_TRAIN_DONE;

Until that completes, `train_ok` never asserts, the peer's SWI_TRAINING_MODE is
never cleared, the GPIO PHY keeps emitting the training pattern, and the Wlink
FCSM cannot leave state 1.

Earlier I2C transactions on the same master DO work in this same run — the mask
exchange (autoneg states 8/9/10), the ST_TRAIN_ENTER write that sets the peer's
swi_training_mode_r = 1, and the ST_TRAIN_POLL_PEER reads that return
peer_lane_locked = 0xFF. So the I2C path is not dead; something about THIS
transaction does not retire.

This samples the transaction's own state machine so the stuck sub-signal can be
named:

    txn_step_r      0=PRESCALE 1=DATA 2=COMMAND 3=POLL 4=CHECK 5=DONE 6=STSCLR
    busy_seen_r     has the I2C master ever been observed BUSY for this txn
    axl_done_r      AXI-Lite handshake done
    axl_rdata_r     captured status word; bit0 = I2C_STS_BUSY, bit3 = MISS_ACK
    mask_byte_cnt_r multi-byte FIFO push counter (TRAIN_MODE_WR_BYTES = 6)
    axl_state_r     the AXI-Lite micro-sequencer

RUN
    make -C sim/het_pair sim MODULE=test_f6_i2cstall
"""
import os

import cocotb
from cocotb.triggers import ClockCycles
from cocotb.utils import get_sim_time

from test_het_pair import Pair, _i

os.environ.setdefault("COCOTB_RESOLVE_X", "ZEROS")

TXN = {0: "PRESCALE", 1: "DATA", 2: "COMMAND", 3: "POLL", 4: "CHECK",
       5: "DONE", 6: "STSCLR"}
ST = {5: "ST_NEGO_DONE", 13: "ST_TRAIN_RUN", 14: "ST_TRAIN_POLL_PEER",
      15: "ST_TRAIN_EXIT", 16: "ST_TRAIN_DONE", 17: "ST_TRAIN_FAIL"}

NETS = ["state_r", "txn_step_r", "busy_seen_r", "axl_done_r", "axl_rdata_r",
        "axl_state_r", "mask_byte_cnt_r", "poll_attempt_r",
        "peer_lane_locked_r", "train_ok_r", "swreset_hold_r"]


def _aneg(dut, die):
    tl = dut.u_dieE.u_tidelink if die == "e" else dut.u_dieC.u_tidelink_0
    return tl.u_chiplet_controller.u_autoneg


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
    dut._log.info(f"I2C-STALL [{tag}]  t={get_sim_time('us'):.1f} us")
    dut._log.info(f"{'u_autoneg net':<30}{'die E (master)':>22}{'die C (slave)':>22}")
    dut._log.info("-" * 76)
    for n in NETS:
        e, c = _v(_aneg(dut, "e"), n), _v(_aneg(dut, "c"), n)
        if n == "txn_step_r":
            e = f"{e} ({TXN.get(e, '?')})" if isinstance(e, int) else e
            c = f"{c} ({TXN.get(c, '?')})" if isinstance(c, int) else c
        if n == "state_r":
            e = f"{e} ({ST.get(e, '?')})" if isinstance(e, int) else e
            c = f"{c} ({ST.get(c, '?')})" if isinstance(c, int) else c
        if n == "axl_rdata_r" and isinstance(e, int):
            e = f"0x{e:08x} busy={e & 1} nack={(e >> 3) & 1}"
        if n == "axl_rdata_r" and isinstance(c, int):
            c = f"0x{c:08x} busy={c & 1} nack={(c >> 3) & 1}"
        dut._log.info(f"{n:<30}{str(e):>22}{str(c):>22}")
    dut._log.info("=" * 76)


@cocotb.test(timeout_time=120, timeout_unit="ms")
async def test_f6_i2c_stall(dut):
    tb = Pair(dut)
    await tb.reset()
    await tb.wait_role_locked()
    await tb.wait_cal_done()
    dut._log.info(f"cal_done both dies at t={get_sim_time('us'):.1f} us")

    # Advance to ST_TRAIN_EXIT, then sample its transaction repeatedly. The
    # master reached 15 at 3148.81 us in test_f6_trainrdv, i.e. ~663 us after
    # cal_done; watch generously past that.
    seen_15_at = None
    changes = 0
    last = {}
    for i in range(300_000):
        await ClockCycles(dut.sys_fclk, 1)
        st = _v(_aneg(dut, "e"), "state_r")
        if st == 15 and seen_15_at is None:
            seen_15_at = get_sim_time("us")
            dut._log.info(f"die E entered ST_TRAIN_EXIT at t={seen_15_at:.2f} us")
            _sweep(dut, "on entry to ST_TRAIN_EXIT")
        if seen_15_at is not None:
            for n in ("state_r", "txn_step_r", "busy_seen_r", "axl_done_r",
                      "axl_state_r", "mask_byte_cnt_r"):
                v = _v(_aneg(dut, "e"), n)
                if last.get(n, "__") != v:
                    if changes < 400:
                        dut._log.info(f"  [{get_sim_time('us'):9.2f} us] die E "
                                      f"{n}: {last.get(n)} -> {v}")
                        changes += 1
                    last[n] = v
            if st == 16:
                dut._log.info(f"die E reached ST_TRAIN_DONE at "
                              f"t={get_sim_time('us'):.2f} us")
                break

    _sweep(dut, "final")
    dut._log.info(f"### total tracked signal changes inside ST_TRAIN_EXIT: {changes}")
    dut._log.info("###   changes == 0 -> the transaction is frozen, not looping")
    dut._log.info("###   changes  > 0 -> it is spinning (TXN_POLL <-> TXN_CHECK)")
    dut._log.info("DIAGNOSTIC COMPLETE (this test always passes; read the log)")
