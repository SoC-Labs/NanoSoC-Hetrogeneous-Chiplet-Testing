"""HET PAIR, MANUAL BRING-UP POSTURE — the F6 workaround that opens the data plane.

Copyright 2026, SoC Labs (www.soclabs.org)

WHY THIS EXISTS
---------------
`test_het_pair` cannot reach FCSM=4: with autonomous negotiation armed,
`ST_TRAIN_EXIT` asserts a Wlink swreset hold that never releases (F6,
docs/F6_ATTRIBUTION.md). Every data-plane test then fails on the link gate
before any of its own logic runs, so the eth<->compute address maps, the
0x2D/0x2A inbound set and inbound confinement are all UNVERIFIED.

But F6 is posture-specific, measured on IDENTICAL dies (docs/F6_ATTRIBUTION.md):

    manual  ROLE_CFG  ->  FCSM 4/4   cal_done   93.6 us
    autoneg NEGO_CFG  ->  FCSM 1/1   cal_done 2567.1 us

The het pair is forced onto autoneg for ONE reason: the compute die exports no
bus (F2), so nothing can write its ROLE_CFG. That is a *silicon* constraint. A
testbench is not a PS — it can reach into the hierarchy, exactly as the shipped
`_calibrator_sim_bypass()` already does with `tb_early_exit_force_q`.

So: drive the ethernet die's ROLE_CFG over its real AHB port, and DEPOSIT the
compute die's two role bits hierarchically. Autoneg stays disarmed
(`+define+HET_PAIR_NO_AUTONEG_PARAM` leaves `NEGO_CFG_RESET` at its shipping
7'h00), the negotiation FSM parks in ST_BYPASS, `ST_TRAIN_EXIT` never runs, and
the swreset hold that F6 is about never happens.

  ==========================================================================
  WHAT THIS DOES AND DOES NOT PROVE
  --------------------------------------------------------------------------
  PROVES      the cross-die DATA PLANE and ADDRESS MAPS: CAM translation, the
              inbound target set, the return path, confinement. Everything
              downstream of "the link is up".
  DOES *NOT*  that this pair can bring itself up. The compute role bits are
  PROVE       deposited by the testbench; on silicon nothing can write them
              (F2) and autoneg is broken (F6). BOTH still block the bench.
  ==========================================================================

Do not cite a pass here as evidence the het link works. It is a data-plane
harness that steps over a known bring-up defect on purpose.

RUN
    make -C sim/het_pair sim MODULE=test_het_manual BUILD=<scratch>/hetman \\
         EXTRA_DEFINES=+define+HET_PAIR_NO_AUTONEG_PARAM
"""
import cocotb
from cocotb.triggers import ClockCycles

from test_het_pair import (  # noqa: E402
    APERTURE_BYTE, C_INBOUND_MAILBOX, C_INBOUND_SRAM, E_INBOUND_MAILBOX,
    NEGO_CFG_AUTONOMOUS, PAYLOAD, ROLE_CFG_MASTER_LOCK, APB_ROLE_CFG,
    Pair, _i,
)

# ROLE_CFG bit map (axi_chiplet_controller.sv:338-339):
#   bit[0] role_cfg_reg  — 0 = master, 1 = slave
#   bit[1] role_lock_reg — W1S, POR-only clear
ROLE_SLAVE = 1
ROLE_LOCK = 1

FCSM_LINK_IDLE = 4


class ManualPair(Pair):
    """`Pair` with the autoneg bring-up replaced by an explicit role lock."""

    def _check_autoneg_armed(self):
        """Inverted: this build must have autoneg DISARMED.

        The base class asserts NEGO_CFG == 0x61. Here the opposite is the
        precondition, and it is worth asserting rather than skipping — running
        this module against an autoneg build would silently re-enter F6 and the
        failure would look like "the manual posture doesn't work either".
        """
        for name, tl in (("die E", self.dut.u_dieE.u_tidelink),
                         ("die C link0", self.dut.u_dieC.u_tidelink_0)):
            got = _i(tl.u_chiplet_controller.nego_cfg_reg, -1)
            assert got != NEGO_CFG_AUTONOMOUS, (
                f"{name}: NEGO_CFG reads 0x{got:02x} — autonegotiation is ARMED. "
                "This module needs it DISARMED. Rebuild with "
                "+define+HET_PAIR_NO_AUTONEG_PARAM.")
        self.log.info("autoneg disarmed on both dies (ST_BYPASS) — manual posture")

    def _calibrator_sim_bypass(self):
        """Early-exit the COMPUTE calibrator only — never the ethernet one.

        MEASURED (test_diag_why_compute_cal_stalls): with the shipped bypass on
        BOTH dies the pair deadlocks. Die E locks all 8 lanes, early-exits, and
        drops `training_mode` to 0 at ~112 us; die C is left at state=2,
        lane_locked=0, training_mode=1 forever, because a receiver can only lock
        while its PEER is still sending training patterns. The master finishes
        first and stops talking before the slave has locked.

        So the master must keep training. Only the slave gets the early exit.
        """
        tl = self.dut.u_dieC.u_tidelink_0
        try:
            tl.u_chiplet_controller.u_calibrator.tb_early_exit_force_q.value = 1
        except AttributeError:
            self.log.warning("die C: tb_early_exit_force_q missing — bypass NOT applied")
        self.log.info("calibrator early-exit: die C only (die E keeps training "
                      "so die C can lock — see _calibrator_sim_bypass docstring)")

    def _force_compute_role_slave(self):
        """Deposit the compute die's role bits — its ROLE_CFG has no bus.

        A DEPOSIT, not a force: in ST_BYPASS neither the APB path nor the
        negotiation FSM writes these registers (axi_chiplet_controller.sv:465-476
        is the only writer and both its arms are gated off), so the value
        persists. A hard force would also mask a regression in which something
        *does* start driving them.
        """
        ctl = self.dut.u_dieC.u_tidelink_0.u_chiplet_controller
        ctl.role_cfg_reg.value = ROLE_SLAVE
        ctl.role_lock_reg.value = ROLE_LOCK
        self.log.info("die C: deposited role_cfg_reg=1 (slave), role_lock_reg=1 "
                      "[TB-side; this die has no bus — F2]")

    async def role_lock_manual(self, settle=200):
        # Ethernet die over its REAL AHB port — no hierarchy games where a bus
        # exists, so breakage in that path still shows up here.
        await self.e.apb_write(APB_ROLE_CFG, ROLE_CFG_MASTER_LOCK)
        await ClockCycles(self.dut.sys_fclk, settle)
        self._force_compute_role_slave()
        await ClockCycles(self.dut.sys_fclk, settle)

        e_locked = _i(self.dut.e_role_locked_o)
        c_locked = _i(self.dut.c_role_locked_o_0)
        assert e_locked and c_locked, (
            f"role_locked did not assert on both dies (E={e_locked} C={c_locked}). "
            "If E is 0 the ethernet APB write did not land; if C is 0 the deposit "
            "did not stick — check role_lock_reg is still the name at "
            "axi_chiplet_controller.sv:339.")
        e_master = _i(self.dut.e_role_is_master_o)
        c_master = _i(self.dut.c_role_is_master_o_0)
        assert e_master == 1 and c_master == 0, (
            f"roles did not resolve opposite: E master={e_master} C master={c_master}")
        self.log.info("roles locked: die E = master, die C = slave")

    async def bring_up_manual(self):
        await self.reset()
        await self.role_lock_manual()
        await self.wait_cal_done()
        await ClockCycles(self.dut.sys_fclk, 5000)

    def assert_link_idle(self):
        e, c = self.fcsm_state("e"), self.fcsm_state("c")
        assert e == FCSM_LINK_IDLE and c == FCSM_LINK_IDLE, (
            f"Wlink FCSM did not reach LINK_IDLE: die E={e} die C={c}. "
            "In the MANUAL posture this should not hit F6 — if it does, F6 is "
            "not posture-specific and docs/F6_ATTRIBUTION.md is wrong.")
        self.log.info(f"LINK UP (manual posture): FCSM E={e} C={c}")


@cocotb.test(timeout_time=90, timeout_unit="ms")
async def test_manual_posture_link_reaches_link_idle(dut):
    """HET-MAN-01: the het pair reaches FCSM=4 when role-locked manually.

    The load-bearing test. If this passes, F6 is confirmed posture-specific and
    every data-plane test below becomes runnable."""
    tb = ManualPair(dut)
    await tb.bring_up_manual()
    assert tb.link_carries_m2s(), "CR/CRACK not seen on the compute die."
    tb.assert_link_idle()


@cocotb.test(timeout_time=90, timeout_unit="ms")
async def test_manual_peer_write_eth_to_compute_sram(dut):
    """HET-MAN-02: an eth-die peer write reaches the COMPUTE die's shared_sram_0.

    CAM 0x2F -> 0x2D — the one inbound byte both designs agree on. The first
    genuinely heterogeneous cross-die transfer, address AND data."""
    tb = ManualPair(dut)
    await tb.bring_up_manual()
    tb.assert_link_idle()
    cocotb.start_soon(tb.catch_compute_inbound())

    await tb.program_eth_cam(APERTURE_BYTE, C_INBOUND_SRAM, enable=True)
    peer_addr = (APERTURE_BYTE << 24) | 0x001000
    await tb.e.write(peer_addr, PAYLOAD)
    await ClockCycles(dut.sys_fclk, 4000)

    inbound = tb.observe_compute_inbound()
    dut._log.info(f"compute inbound beats = {tb.fmt_beats()}")
    assert (inbound >> 24) == C_INBOUND_SRAM, (
        f"compute inbound saw 0x{inbound:08x}; expected upper byte "
        f"0x{C_INBOUND_SRAM:02x} (CAM should rewrite "
        f"0x{APERTURE_BYTE:02x}->0x{C_INBOUND_SRAM:02x})")
    assert not tb.saw_error_response(), (
        f"the compute die ERRORed a write to 0x{C_INBOUND_SRAM:02X}, which IS in "
        f"its inbound target set — a real fault. beats={tb.fmt_beats()}")

    rb = await tb.e.read(peer_addr)
    assert rb == PAYLOAD, (
        f"peer read-back returned 0x{rb:08x}, expected 0x{PAYLOAD:08x} — the "
        "address reached the compute die but the data did not survive the "
        "round trip.")
    dut._log.info(f"DATA ok: eth 0x{peer_addr:08x} -> compute SRAM, read back "
                  f"0x{rb:08x}")


@cocotb.test(timeout_time=90, timeout_unit="ms")
async def test_manual_mailbox_uses_compute_byte(dut):
    """HET-MAN-03: the mailbox is reachable at the COMPUTE die's byte, 0x2A.

    The defining heterogeneous asymmetry. The eth die's own mailbox is 0x23; the
    compute die moved it to 0x2A because 0x22-0x23 is its Cortex-M4 bit-band
    alias (nanosoc_compute_soc.yaml:989)."""
    tb = ManualPair(dut)
    await tb.bring_up_manual()
    tb.assert_link_idle()
    cocotb.start_soon(tb.catch_compute_inbound())

    await tb.program_eth_cam(APERTURE_BYTE, C_INBOUND_MAILBOX, enable=True)
    await tb.e.write((APERTURE_BYTE << 24) | 0x000000, PAYLOAD)
    await ClockCycles(dut.sys_fclk, 4000)

    inbound = tb.observe_compute_inbound()
    dut._log.info(f"compute inbound beats = {tb.fmt_beats()}")
    assert (inbound >> 24) == C_INBOUND_MAILBOX, (
        f"compute inbound saw 0x{inbound:08x}; expected upper byte "
        f"0x{C_INBOUND_MAILBOX:02x}")
    assert not tb.saw_error_response(), (
        f"the compute die ERRORed a write to 0x{C_INBOUND_MAILBOX:02X}, which IS "
        f"in its inbound target set — a real fault. beats={tb.fmt_beats()}")
    dut._log.info(f"mailbox OK via 0x{C_INBOUND_MAILBOX:02X}")


@cocotb.test(timeout_time=90, timeout_unit="ms")
async def test_manual_eth_mailbox_byte_is_confined_on_compute(dut):
    """HET-MAN-04: the ETH mailbox byte (0x23) must be REFUSED by the compute die.

    Inbound confinement — untested anywhere in either repo until now. The only
    DECERR test that exists is outbound (`tb_tx_gate.sv`). 0x23 is not in the
    compute die's inbound target set, so its default slave must ERROR the write
    rather than let it retire somewhere. This is the case a CAM rule copied
    verbatim from the homogeneous pair would produce."""
    tb = ManualPair(dut)
    await tb.bring_up_manual()
    tb.assert_link_idle()
    cocotb.start_soon(tb.catch_compute_inbound())

    await tb.program_eth_cam(APERTURE_BYTE, E_INBOUND_MAILBOX, enable=True)
    await tb.e.write((APERTURE_BYTE << 24) | 0x000000, 0xDEADBEEF)
    await ClockCycles(dut.sys_fclk, 4000)

    inbound = tb.observe_compute_inbound()
    dut._log.info(f"compute inbound beats = {tb.fmt_beats()}")
    assert (inbound >> 24) == E_INBOUND_MAILBOX, (
        f"the CAM did not present 0x{E_INBOUND_MAILBOX:02X} at the compute "
        f"inbound port (saw 0x{inbound:08x}); the confinement case never ran")
    assert tb.saw_error_response(), (
        f"the compute die ACCEPTED a write to 0x{E_INBOUND_MAILBOX:02X}, which is "
        "NOT in its inbound target set (only 0x2D and 0x2A are). Inbound "
        f"confinement is broken. beats={tb.fmt_beats()}")
    dut._log.info(f"CONFINED: 0x{E_INBOUND_MAILBOX:02X} refused by the compute "
                  "default slave, as required")


@cocotb.test(timeout_time=40, timeout_unit="ms")
async def test_diag_why_compute_cal_stalls(dut):
    """DIAG (not a gate): is the compute calibrator being CLOCKED at all?

    Its clock is the peer's forwarded RX link clock
    (axi_chiplet_controller: .clk(phy_link_rx_rx_link_clk_w)), so if die E is not
    transmitting, die C's calibrator never advances and its state reads X — which
    is exactly what the manual-posture run reports (cur_state=-1)."""
    tb = ManualPair(dut)
    await tb.reset()
    await tb.role_lock_manual()

    e_cal = tb.dut.u_dieE.u_tidelink.u_chiplet_controller.u_calibrator
    c_cal = tb.dut.u_dieC.u_tidelink_0.u_chiplet_controller.u_calibrator

    def snap(tag):
        row = {}
        for nm, node in (("E", e_cal), ("C", c_cal)):
            for sig in ("cur_state", "state", "calibration_done", "training_mode",
                        "lane_locked", "role_locked", "rst", "swreset"):
                if hasattr(node, sig):
                    row[f"{nm}.{sig}"] = _i(getattr(node, sig), -1)
        row["E.pad_clk_tx"] = _i(tb.dut.e_pad_clk_tx, -1)
        row["C.pad_clk_tx"] = _i(tb.dut.c_pad_clk_tx_0, -1)
        dut._log.info(f"DIAG[{tag}] " + " ".join(f"{k}={v}" for k, v in row.items()))
        return row

    edges = {"E": 0, "C": 0}
    prev = {"E": _i(tb.dut.e_pad_clk_tx, -1), "C": _i(tb.dut.c_pad_clk_tx_0, -1)}
    for i in range(12):
        for _ in range(200):
            await ClockCycles(tb.dut.sys_fclk, 5)
            for k, sig in (("E", tb.dut.e_pad_clk_tx), ("C", tb.dut.c_pad_clk_tx_0)):
                v = _i(sig, -1)
                if v != prev[k]:
                    edges[k] += 1
                    prev[k] = v
        snap(f"t{i}")
        dut._log.info(f"DIAG[t{i}] pad_clk_tx edges so far: E={edges['E']} C={edges['C']}")
    dut._log.info("DIAG COMPLETE — a die whose pad_clk_tx never toggles is not "
                  "transmitting, so the PEER's calibrator can never clock.")
