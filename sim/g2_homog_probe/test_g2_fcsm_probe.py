"""F6 CELL-B DIAGNOSTIC — does the HOMOGENEOUS chiplet pair reach FCSM state 4?

Copyright 2026, SoC Labs (www.soclabs.org)

This is the control the F6 attribution turns on. `sim/het_pair` (two DIFFERENT
chiplet tops) stalls the Wlink flow-control state machine at state 1. The
ethernet repo's own `verif/g2_soc_pair` runs two IDENTICAL ethernet dies through
the same TideLink — but it asserts on `cr_pkt_seen_rx & crack_pkt_seen_rx`, NOT
on the FCSM, so a green run there says nothing about state 4. This module reads
the FCSM directly.

It also splits the two variables the het bench changed at once:

  BRING-UP POSTURE                       reachable on...
  ----------------------------------     ----------------------------------
  manual: APB ROLE_CFG + LL bootstrap    homogeneous pair only (the compute
                                         die has no bus — SIM_PLAN F2)
  autonomous: NEGO_CFG=0x61, no LL       both

`test_het_pair` had to use the autonomous posture. So "het pair stalls" conflates
"heterogeneous" with "autonomous, no LL bootstrap". The three testcases below
run on the SAME homogeneous pair and differ in ONE variable each:

  b1_manual_with_ll      shipped g2_soc_pair recipe          (posture: manual)
  b2_manual_no_ll        identical, LL bootstrap REMOVED     (isolates the LL
                         bootstrap: SIM_PLAN claims it is a no-op from cold POR)
  b3_autoneg_no_ll       NEGO_CFG=0x61 poked, no ROLE_CFG,   (the het posture,
                         no LL bootstrap                      homogeneous dies)

Each testcase must be run in its OWN sim: a second bring-up inside one sim does
not re-converge cal_done (test_g2_soc_pair.py:307).

RUN (from this directory's sibling Makefile, or directly):

    make -C <eth-chiplet>/verif/g2_soc_pair sim \\
         BUILD=<scratch>/g2b1 MODULE=test_g2_fcsm_probe \\
         TESTCASE=test_b1_manual_with_ll \\
         PYTHONPATH=<repo>/sim/g2_homog_probe
"""
import os

import cocotb
from cocotb.triggers import ClockCycles
from cocotb.utils import get_sim_time

# Resolved from the g2_soc_pair directory, which cocotb puts on sys.path.
# The manual posture is driven through Pair's own helpers (`to_data_mode()`
# does R8_SLOT0 + the 3-write LL bootstrap; roles go through the reset helper),
# so the individual register constants are NOT imported here — importing them
# would only invite a second, divergent copy of the recipe.
from test_g2_soc_pair import APB_R8_SWI_LANE_STATUS, Pair  # noqa: E402

os.environ.setdefault("COCOTB_RESOLVE_X", "ZEROS")

TLAPB_BASE  = 0x2E03_0000
APB_NEGO_CFG = TLAPB_BASE + 0x2090   # tidelink REGISTER_MAP.md — NEGO_CFG
NEGO_CFG_AUTONOMOUS = 0x61


def _i(sig, default=-1):
    try:
        v = sig.value
        return int(v) if v.is_resolvable else default
    except Exception:
        return default


def _fcsm(dut, die):
    return getattr(dut, f"u_die{die}").u_tidelink.u_chiplet_controller \
        .u_wlink.tl2wl.wlink_tidelinktl


PROBES = ["state", "io_app_enable", "en_ff2_tx_demet_io_out",
          "auto_tx_out_advance", "auto_rx_in_valid",
          "cr_pkt_seen_rx", "crack_pkt_seen_rx",
          "cr_pkt_seen_tx_demet_io_out", "crack_pkt_seen_tx_demet_io_out",
          "socl_l6_cr_emit_count", "socl_l6_cr_emit_gate_ok"]


def _report(dut, tag):
    dut._log.info("=" * 72)
    dut._log.info(f"CELL-B PROBE [{tag}]  t={get_sim_time('us'):.1f} us")
    dut._log.info(f"{'signal':<34}{'die A (master)':>18}{'die B (slave)':>18}")
    dut._log.info("-" * 72)
    got = {}
    for n in PROBES:
        a = _i(getattr(_fcsm(dut, "A"), n, None)) if hasattr(_fcsm(dut, "A"), n) else "-"
        b = _i(getattr(_fcsm(dut, "B"), n, None)) if hasattr(_fcsm(dut, "B"), n) else "-"
        got[n] = (a, b)
        dut._log.info(f"{n:<34}{str(a):>18}{str(b):>18}")
    dut._log.info("=" * 72)
    a, b = got["state"]
    dut._log.info(f"### CELL B RESULT [{tag}]: FCSM dieA={a} dieB={b}  "
                  f"(4 == LINK_IDLE == 'the link is up')")
    return got


async def _wait_cal_done(tb, max_cycles=500_000):
    """`Pair.wait_cal_done` budgets only 100*100 = 10k cycles. That is enough for
    the manual posture (measured: cal_done at 89.8 us) but NOT for the autonomous
    one, which is far slower to converge. Use the proven upstream budget
    (tidelink/cocotb/tidelink_top_pair_v2/pair_v2_common.py:252) so a slow-but-
    converging link is not misreported as a failure — exactly the trap
    docs/SIM_PLAN.md 8a records."""
    for _ in range(max_cycles // 200):
        m = await tb.a.apb_read(APB_R8_SWI_LANE_STATUS)
        s = await tb.b.apb_read(APB_R8_SWI_LANE_STATUS)
        if ((m >> 16) & 1) and ((s >> 16) & 1):
            return
        await ClockCycles(tb.dut.sys_fclk, 200)
    raise TimeoutError(
        f"cal_done never asserted within {max_cycles} cycles "
        f"(dieA R8_SWI_LANE_STATUS=0x{m:08x}, dieB=0x{s:08x})")


async def _common_reset_and_roles(tb, autoneg=False):
    await tb.reset()
    if autoneg:
        # Arm autonomous negotiation the only way an already-reset design can be
        # armed: an APB write. SIM_PLAN 8a records that on the het pair the poke
        # and the time-0 defparam give identical results through cal_done and
        # identical FCSM stall, so this is a faithful stand-in for the parameter.
        await tb.a.apb_write(APB_NEGO_CFG, NEGO_CFG_AUTONOMOUS)
        await tb.b.apb_write(APB_NEGO_CFG, NEGO_CFG_AUTONOMOUS)
        for _ in range(4000):
            if _i(tb.dut.a_role_locked_o, 0) and _i(tb.dut.b_role_locked_o, 0):
                break
            await ClockCycles(tb.dut.sys_fclk, 50)
        else:
            raise TimeoutError("autoneg: role_locked never asserted on both dies")
    else:
        await tb.role_lock()
    tb.log.info(f"role_locked both dies at t={get_sim_time('us'):.1f} us")
    await _wait_cal_done(tb)
    tb.log.info(f"cal_done both dies at t={get_sim_time('us'):.1f} us")


async def _settle_and_report(dut, tb, tag, cycles=20000):
    _report(dut, f"{tag}: at cal_done")
    await ClockCycles(dut.sys_fclk, cycles)
    got = _report(dut, f"{tag}: +{cycles} cycles")
    a, b = got["state"]
    dut._log.info(f"link_carries_m2s (cr+crack on die B) = {tb.link_carries_m2s()}")
    dut._log.info(f"DIAGNOSTIC COMPLETE [{tag}] — FCSM A={a} B={b}")


@cocotb.test(timeout_time=60, timeout_unit="ms")
async def test_b1_manual_with_ll(dut):
    """The shipped g2_soc_pair recipe, verbatim. Reads the FCSM, which the
    shipped test never does."""
    tb = Pair(dut)
    await _common_reset_and_roles(tb, autoneg=False)
    await tb.to_data_mode()          # R8_SLOT0 + the 3-write LL bootstrap
    await _settle_and_report(dut, tb, "b1 manual + LL bootstrap")


@cocotb.test(timeout_time=60, timeout_unit="ms")
async def test_b2_manual_no_ll(dut):
    """One variable removed from b1: the LL bootstrap. SIM_PLAN 5 asserts the
    triplet is a no-op from cold POR (LL_ENABLE == the reset value of the fields
    it writes). If b1 reaches 4 and b2 does not, that claim is false and the LL
    bootstrap — not heterogeneity — is what the het pair is missing."""
    tb = Pair(dut)
    await _common_reset_and_roles(tb, autoneg=False)
    await ClockCycles(dut.sys_fclk, 5000)   # same settle the het bench uses
    await _settle_and_report(dut, tb, "b2 manual, NO LL bootstrap")


@cocotb.test(timeout_time=60, timeout_unit="ms")
async def test_b3_autoneg_no_ll(dut):
    """The het bench's exact posture on IDENTICAL dies. If this stalls at 1 the
    het pair's stall is not about heterogeneity at all."""
    tb = Pair(dut)
    await _common_reset_and_roles(tb, autoneg=True)
    await ClockCycles(dut.sys_fclk, 5000)
    await _settle_and_report(dut, tb, "b3 autoneg, NO LL bootstrap")


@cocotb.test(timeout_time=60, timeout_unit="ms")
async def test_b4_autoneg_with_ll(dut):
    """The fourth corner: autonomous negotiation AND the LL bootstrap. On the het
    pair this hangs the AHB matrix (SIM_PLAN F5). Recorded here to establish
    whether that hang is heterogeneity-specific or intrinsic to the posture."""
    tb = Pair(dut)
    await _common_reset_and_roles(tb, autoneg=True)
    await tb.to_data_mode()
    await _settle_and_report(dut, tb, "b4 autoneg + LL bootstrap")
