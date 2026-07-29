"""F6 DIAGNOSTIC — why does the Wlink FCSM stall at state 1 on the het pair?

Copyright 2026, SoC Labs (www.soclabs.org)

NOT a pass/fail test. This is an instrument: it runs the identical bring-up
`test_het_pair.Pair.bring_up()` performs, then samples the FCSM and everything
that gates its state-1 exit, on BOTH dies, and prints a table. It ends with an
explicit PASS so a run is never mistaken for a regression.

WHAT STATE 1 IS WAITING FOR (WlinkGenericFCSM_6.v:1139-1157)

    state 0 --(en_ff2_tx_demet_io_out, i.e. io_app_enable)--> state 1
    state 1 --(auto_tx_out_advance & _GEN_34)--------------> state 2
      _GEN_34 = (crack_pkt_seen_tx_demet | cr_pkt_seen_tx_demet)
                & socl_l6_cr_emit_gate_ok            (:719)
      socl_l6_cr_emit_gate_ok = socl_l6_cr_emit_count >= SOCL_L6_MIN_CR_EMITS
      socl_l6_cr_emit_count increments on (auto_tx_out_advance & sop & state==1)

So a die parked in state 1 is short of exactly one of three things:
  (a) auto_tx_out_advance never pulses  -> it is emitting NOTHING; the TX link
      layer is not accepting packets. socl_l6_cr_emit_count stays 0.
  (b) cr/crack_pkt_seen_rx never latch  -> its RX framer never decoded a CR or
      CRACK from the peer. count will be pinned at 0xff (saturated).
  (c) the L6 gate is short                -> count < 32 but nonzero.

Those three are mutually exclusive in the sampled data, which is the point:
the counter's value alone discriminates (a) from (b).

RUN
    make -C sim/het_pair sim MODULE=test_f6_diag
"""
import os

import cocotb
from cocotb.triggers import ClockCycles
from cocotb.utils import get_sim_time

from test_het_pair import Pair, _i

os.environ.setdefault("COCOTB_RESOLVE_X", "ZEROS")


def _fcsm(dut, die):
    tl = dut.u_dieE.u_tidelink if die == "e" else dut.u_dieC.u_tidelink_0
    return tl.u_chiplet_controller.u_wlink.tl2wl.wlink_tidelinktl


def _wlink(dut, die):
    tl = dut.u_dieE.u_tidelink if die == "e" else dut.u_dieC.u_tidelink_0
    return tl.u_chiplet_controller.u_wlink


# name -> (handle-path within wlink_tidelinktl). Sampled defensively: a signal
# that does not exist on one die's TideLink revision reports "-" rather than
# aborting the sweep, which is itself a result worth seeing.
FCSM_PROBES = [
    "state",
    "io_app_enable",
    "en_ff2_tx_demet_io_out",
    "auto_tx_out_advance",
    "auto_tx_out_sop",
    "auto_tx_out_data_id",
    "auto_rx_in_valid",
    "auto_rx_in_sop",
    "auto_rx_in_data_id",
    "pkt_is_cr_pkt",
    "pkt_is_crack_pkt",
    "cr_pkt_seen_rx",
    "crack_pkt_seen_rx",
    "cr_pkt_seen_tx_demet_io_out",
    "crack_pkt_seen_tx_demet_io_out",
    "socl_l6_cr_emit_count",
    "socl_l6_cr_emit_gate_ok",
    "swi_cr_id",
    "swi_data_id_1",
    "io_tx_reset",
    "io_rx_reset",
    "io_app_reset",
    "reset",
]


def _probe(obj, name):
    try:
        h = getattr(obj, name)
    except AttributeError:
        return "-"
    try:
        v = h.value
    except Exception:
        return "?"
    if not v.is_resolvable:
        return f"X({v.binstr})"
    return int(v)


def _sweep(dut, tag):
    dut._log.info("=" * 78)
    dut._log.info(f"F6 PROBE [{tag}]  t={get_sim_time('us'):.1f} us")
    dut._log.info(f"{'signal':<34}{'die E (eth/master)':>21}{'die C (cmp/slave)':>21}")
    dut._log.info("-" * 78)
    out = {}
    for name in FCSM_PROBES:
        e = _probe(_fcsm(dut, "e"), name)
        c = _probe(_fcsm(dut, "c"), name)
        out[name] = (e, c)
        dut._log.info(f"{name:<34}{str(e):>21}{str(c):>21}")
    dut._log.info("=" * 78)
    return out


# Pads: is anything physically moving between the dies at all?
async def _pad_activity(dut, cycles=2000):
    """Count edges on each direction's PHY pads over `cycles` sys_fclk cycles."""
    prev_e = prev_c = None
    e_edges = c_edges = 0
    e_clk_edges = c_clk_edges = 0
    prev_ec = prev_cc = None
    for _ in range(cycles):
        await ClockCycles(dut.sys_fclk, 1)
        v = _probe(dut, "e_pad_tx")
        if isinstance(v, int):
            if prev_e is not None and v != prev_e:
                e_edges += 1
            prev_e = v
        v = _probe(dut, "c_pad_tx_0")
        if isinstance(v, int):
            if prev_c is not None and v != prev_c:
                c_edges += 1
            prev_c = v
        v = _probe(dut, "e_pad_clk_tx")
        if isinstance(v, int):
            if prev_ec is not None and v != prev_ec:
                e_clk_edges += 1
            prev_ec = v
        v = _probe(dut, "c_pad_clk_tx_0")
        if isinstance(v, int):
            if prev_cc is not None and v != prev_cc:
                c_clk_edges += 1
            prev_cc = v
    dut._log.info(
        f"PAD ACTIVITY over {cycles} sys_fclk cycles: "
        f"eth pad_tx transitions={e_edges} pad_clk_tx={e_clk_edges} | "
        f"cmp pad_tx_0 transitions={c_edges} pad_clk_tx_0={c_clk_edges}")


@cocotb.test(timeout_time=60, timeout_unit="ms")
async def test_f6_probe_fcsm_stall(dut):
    tb = Pair(dut)

    await tb.reset()
    _sweep(dut, "after reset")

    await tb.wait_role_locked()
    dut._log.info(f"role_locked both dies at t={get_sim_time('us'):.1f} us")
    _sweep(dut, "after role_locked")

    await tb.wait_cal_done()
    dut._log.info(f"cal_done both dies at t={get_sim_time('us'):.1f} us")
    s = _sweep(dut, "after cal_done")

    # Watch the FCSM for a long window and record every state change and the
    # first time each gate term asserts. This is the evidence: a term that
    # never changes over ~100k cycles after cal_done is the stuck one.
    firsts = {}
    last_state = {"e": None, "c": None}
    WATCH = 40_000
    for i in range(WATCH):
        await ClockCycles(dut.sys_fclk, 1)
        for die in ("e", "c"):
            f = _fcsm(dut, die)
            st = _probe(f, "state")
            if st != last_state[die]:
                dut._log.info(f"  [{get_sim_time('us'):9.2f} us] die {die.upper()} "
                              f"FCSM state {last_state[die]} -> {st}")
                last_state[die] = st
            for term in ("auto_tx_out_advance", "cr_pkt_seen_rx",
                         "crack_pkt_seen_rx", "cr_pkt_seen_tx_demet_io_out",
                         "crack_pkt_seen_tx_demet_io_out",
                         "socl_l6_cr_emit_gate_ok", "auto_rx_in_valid",
                         "pkt_is_cr_pkt", "pkt_is_crack_pkt"):
                k = (die, term)
                if k not in firsts and _probe(f, term) == 1:
                    firsts[k] = get_sim_time("us")
                    dut._log.info(f"  [{get_sim_time('us'):9.2f} us] die {die.upper()} "
                                  f"FIRST ASSERT: {term}")

    dut._log.info("")
    dut._log.info("### FIRST-ASSERTION TABLE (blank = NEVER asserted in the window)")
    for term in ("auto_rx_in_valid", "auto_tx_out_advance", "pkt_is_cr_pkt",
                 "pkt_is_crack_pkt", "cr_pkt_seen_rx", "crack_pkt_seen_rx",
                 "cr_pkt_seen_tx_demet_io_out", "crack_pkt_seen_tx_demet_io_out",
                 "socl_l6_cr_emit_gate_ok"):
        e = firsts.get(("e", term))
        c = firsts.get(("c", term))
        dut._log.info(f"  {term:<34} die E: {('%.2f us' % e) if e else 'NEVER':>12}"
                      f"   die C: {('%.2f us' % c) if c else 'NEVER':>12}")

    s = _sweep(dut, f"after {WATCH} more cycles")
    await _pad_activity(dut, 2000)

    e_st, c_st = s["state"]
    e_cnt, c_cnt = s["socl_l6_cr_emit_count"]
    dut._log.info("")
    dut._log.info("### VERDICT INPUTS")
    dut._log.info(f"  FCSM state            eth={e_st}  compute={c_st}")
    dut._log.info(f"  socl_l6_cr_emit_count eth={e_cnt} compute={c_cnt}")
    dut._log.info("  count==0    -> auto_tx_out_advance never pulses (TX LL not accepting)")
    dut._log.info("  count==0xff -> emitting fine, peer CR/CRACK never decoded (RX framer)")
    dut._log.info("  0<count<32  -> L6 gate short")
    dut._log.info("DIAGNOSTIC COMPLETE (this test always passes; read the log)")
