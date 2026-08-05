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
    NEGO_CFG_AUTONOMOUS, PAYLOAD, ROLE_CFG_MASTER_LOCK, ROLE_CFG_SLAVE_LOCK,
    APB_ROLE_CFG, Pair, _i,
)

# IPC mailbox slot-0 layout (nanosoc_multicore_addrmap.h, mirrored in
# kr260_eth_xfer.py:65-69). Identical on both dies — only the BASE byte differs
# (eth 0x23, compute 0x2A).
IPC_SLOT0_DATA = 0x000        # .. +0x00C, four words
IPC_SLOT0_CTRL = 0x020        # [0] = MSG_VALID, [1] = ACK
IPC_MSG_VALID = 1 << 0

# ROLE_CFG bit map (axi_chiplet_controller.sv:338-339):
#   bit[0] role_cfg_reg  — 0 = master, 1 = slave
#   bit[1] role_lock_reg — W1S, POR-only clear
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

    async def role_lock_manual(self, settle=200):
        """Lock both roles. Prefers the REAL bus; falls back to a deposit.

        compute-chiplet G2 (1a9ab1b) re-exported `ps_ahb_s`, so die C should be
        drivable exactly like die E. It is not, quite — see G2-GAP below — so
        this tries the real path first and only deposits if that fails. When the
        gap closes, this method starts using the real bus with no edit here, and
        the WARNING below stops appearing.

        ---------------------------------------------------------------------
        G2-GAP (MEASURED 2026-07-29, test_diag_ps_ahb_s_reaches_compute)

        `ps_ahb_s` works — a write/read-back to compute `shared_sram_0`
        @0x2D002000 returns 0xa5a5beef. But every access to the D2D window
        reads 0x00000000 and the role never latches:

            0x40032080 ROLE_CFG    -> 0x00000000
            0x40032084 ROLE_STATUS -> 0x00000000   (after writing 0x03)
            0x40032108 SWI_LANE    -> 0x00000000
            c_role_locked_o_0      -> 0

        Cause: `ps_m`'s target list in nanosoc_compute_soc.yaml:1110 omits
        `d2d0` and `d2d1`. `manager_m`, `compute_m` and `dma_250_0_m` all have
        them; `ps_m` does not. So the backdoor reaches the whole INTERNAL map
        and none of the D2D window — no TideLink APB, no CAM, no peer aperture.
        That contradicts the same file's description at :104, "becomes top-matrix
        initiator ps_m reaching the whole compute map".

        Consequence: host-side bring-up of the compute die is impossible on
        silicon until `d2d0`/`d2d1` are added to that list. It is a yaml change
        in the compute repo, not something this repo can or should patch.
        ---------------------------------------------------------------------
        """
        await self.e.apb_write(APB_ROLE_CFG, ROLE_CFG_MASTER_LOCK)
        await self.c.apb_write(APB_ROLE_CFG, ROLE_CFG_SLAVE_LOCK)
        await ClockCycles(self.dut.sys_fclk, settle)

        if not _i(self.dut.c_role_locked_o_0):
            self.log.warning(
                "die C role did NOT lock over ps_ahb_s — falling back to a "
                "hierarchical DEPOSIT. This is the G2-GAP: ps_m cannot reach "
                "d2d0 (nanosoc_compute_soc.yaml:1110 target list). The result "
                "below therefore still carries the testbench-crutch caveat.")
            ctl = self.dut.u_dieC.u_tidelink_0.u_chiplet_controller
            ctl.role_cfg_reg.value = 1     # slave
            ctl.role_lock_reg.value = 1    # W1S lock
            await ClockCycles(self.dut.sys_fclk, settle)
        else:
            self.log.info("die C role locked over the REAL ps_ahb_s bus — "
                          "G2-GAP is closed, no deposit needed")

        e_locked = _i(self.dut.e_role_locked_o)
        c_locked = _i(self.dut.c_role_locked_o_0)
        assert e_locked and c_locked, (
            f"role_locked did not assert on both dies (E={e_locked} C={c_locked}). "
            "If E is 0 the ethernet APB write did not land; if C is 0 BOTH the "
            "ps_ahb_s write and the deposit fallback failed — check role_lock_reg "
            "is still the name at axi_chiplet_controller.sv:339.")
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
    """L0-SIM-02: the het pair reaches FCSM=4 when role-locked manually.

    The load-bearing test. If this passes, F6 is confirmed posture-specific and
    every data-plane test below becomes runnable."""
    tb = ManualPair(dut)
    await tb.bring_up_manual()
    assert tb.link_carries_m2s(), "CR/CRACK not seen on the compute die."
    tb.assert_link_idle()


@cocotb.test(timeout_time=90, timeout_unit="ms")
async def test_manual_peer_write_eth_to_compute_sram(dut):
    """L0-SIM-03: an eth-die peer write reaches the COMPUTE die's shared_sram_0.

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
    """L0-SIM-05: the mailbox is reachable at the COMPUTE die's byte, 0x2A.

    The defining heterogeneous asymmetry. The eth die's own mailbox is 0x23; the
    compute die moved it to 0x2A because 0x22-0x23 is its Cortex-M4 bit-band
    alias (nanosoc_compute_soc.yaml:989)."""
    tb = ManualPair(dut)
    await tb.bring_up_manual()
    tb.assert_link_idle()
    cocotb.start_soon(tb.catch_compute_inbound())

    await tb.program_eth_cam(APERTURE_BYTE, C_INBOUND_MAILBOX, enable=True)
    base = (APERTURE_BYTE << 24)

    # The REAL mailbox protocol, not just one write: four payload words, then
    # the doorbell. Earlier this test wrote a single word at offset 0 and
    # stopped, which is why it never caught the defect below.
    words = [PAYLOAD ^ (i * 0x1111_1111) for i in range(4)]
    for i, w in enumerate(words):
        await tb.e.write(base + IPC_SLOT0_DATA + 4 * i, w)
    await tb.e.write(base + IPC_SLOT0_CTRL, IPC_MSG_VALID)   # <- the doorbell
    await ClockCycles(dut.sys_fclk, 4000)

    beats = tb.beats()
    dut._log.info(f"compute inbound beats = {tb.fmt_beats()}")
    assert not tb.saw_error_response(), (
        f"the compute die ERRORed a write to 0x{C_INBOUND_MAILBOX:02X}, which IS "
        f"in its inbound target set — a real fault. beats={tb.fmt_beats()}")

    landed = {a & 0xFFFF: d for a, d, _e in beats}
    for i, w in enumerate(words):
        off = IPC_SLOT0_DATA + 4 * i
        assert landed.get(off) == w, (
            f"slot-0 word {i} @+0x{off:03X}: saw 0x{landed.get(off, 0):08x}, "
            f"wrote 0x{w:08x}")

    # THE DOORBELL. This is an ISOLATED single write following a burst, and it
    # is the bit the receiver actually waits on: without MSG_VALID the far side
    # never knows a message arrived, so a "passing" mailbox test that omits it
    # proves only that bytes moved, not that the IPC works.
    #
    # docs/CHIPLET_ALIGNMENT_AUDIT.md reports that isolated D2D writes deliver
    # 0x00000000. If that holds here the assertion below fires with the observed
    # value, which is the evidence needed either way.
    doorbell = landed.get(IPC_SLOT0_CTRL)
    assert doorbell is not None, (
        f"the doorbell write to +0x{IPC_SLOT0_CTRL:03X} never reached the "
        f"compute inbound port at all. beats={tb.fmt_beats()}")
    assert doorbell & IPC_MSG_VALID, (
        f"the doorbell arrived as 0x{doorbell:08x}, MSG_VALID clear — the "
        f"receiver will never see the message. Expected bit0 set "
        f"(wrote 0x{IPC_MSG_VALID:08x}).\n"
        "  If this reads 0x00000000 it is the isolated-D2D-write defect in "
        "docs/CHIPLET_ALIGNMENT_AUDIT.md: the data burst lands but the lone "
        "following write delivers zeros.\n"
        f"  beats={tb.fmt_beats()}")
    dut._log.info(f"mailbox OK via 0x{C_INBOUND_MAILBOX:02X}: 4 words + "
                  f"doorbell 0x{doorbell:08x}")


@cocotb.test(timeout_time=90, timeout_unit="ms")
async def test_manual_eth_mailbox_byte_is_confined_on_compute(dut):
    """L0-SIM-08: the ETH mailbox byte (0x23) must be REFUSED by the compute die.

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


@cocotb.test(timeout_time=90, timeout_unit="ms")
async def test_manual_cam_disabled_is_identity(dut):
    """L0-SIM-07: with the CAM off, the aperture byte must arrive UNTRANSLATED.

    The control for L0-SIM-03/05. Without it those tests only show that 0x2D
    arrived — not that the CAM is what put it there. If this fails, the
    translated byte in every other test came from somewhere else and the whole
    address-map story is wrong."""
    tb = ManualPair(dut)
    await tb.bring_up_manual()
    tb.assert_link_idle()
    cocotb.start_soon(tb.catch_compute_inbound())

    await tb.program_eth_cam(APERTURE_BYTE, C_INBOUND_SRAM, enable=False)
    peer_addr = (APERTURE_BYTE << 24) | 0x003000
    await tb.e.write(peer_addr, PAYLOAD ^ 0xFFFF)
    await ClockCycles(dut.sys_fclk, 4000)

    inbound = tb.observe_compute_inbound()
    dut._log.info(f"compute inbound beats = {tb.fmt_beats()}")
    assert (inbound >> 24) == APERTURE_BYTE, (
        f"CAM disabled, but the compute die inbound saw 0x{inbound:08x} — "
        f"expected the identity map (upper byte 0x{APERTURE_BYTE:02x}). The "
        "translated byte in the other tests did NOT come from the CAM.")
    dut._log.info(f"CONTROL ok: CAM off -> inbound 0x{inbound:08x} (identity)")


@cocotb.test(timeout_time=90, timeout_unit="ms")
async def test_manual_peer_sequence_eth_to_compute(dut):
    """L0-SIM-10: 8 consecutive words cross the aperture intact.

    Catches cross-beat corruption a single access cannot: a write-data delay
    misaligning beat N with N+1, or a read pipe-offset that fails to re-arm.
    This is what a memcpy across the peer aperture actually does."""
    tb = ManualPair(dut)
    await tb.bring_up_manual()
    tb.assert_link_idle()

    await tb.program_eth_cam(APERTURE_BYTE, C_INBOUND_SRAM, enable=True)
    base = (APERTURE_BYTE << 24) | 0x002000
    seq = [(base + 4 * i, 0x5EED0000 + (i << 4) + i) for i in range(8)]

    for addr, val in seq:
        await tb.e.write(addr, val)
    await ClockCycles(dut.sys_fclk, 4000)

    bad = []
    for addr, val in seq:
        rb = await tb.e.read(addr)
        if rb != val:
            bad.append((addr, val, rb))
    assert not bad, (
        "sequence corrupted across the heterogeneous aperture: "
        + ", ".join(f"0x{a:08x} wrote 0x{w:08x} read 0x{r:08x}" for a, w, r in bad))
    dut._log.info(f"SEQ ok: {len(seq)} words intact across the het aperture")



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


@cocotb.test(timeout_time=30, timeout_unit="ms")
async def test_diag_ps_ahb_s_reaches_compute(dut):
    """DIAG: does ps_ahb_s actually deliver into the compute SoC?

    Before trusting it as the role-lock path, prove a plain read/write lands.
    Reads a few compute-side registers over the new port and reports raw values
    rather than asserting, so the failure mode is visible instead of binary."""
    tb = ManualPair(dut)
    await tb.reset()

    from test_het_pair import C_TLAPB_BASE
    probes = [
        ("tlapb ROLE_CFG    0x2080", C_TLAPB_BASE + 0x2080),
        ("tlapb ROLE_STATUS 0x2084", C_TLAPB_BASE + 0x2084),
        ("tlapb SWI_LANE    0x2108", C_TLAPB_BASE + 0x2108),
        ("compute shared_sram 0x2D001000", 0x2D001000),
    ]
    for name, addr in probes:
        try:
            v = await tb.c.read(addr)
            dut._log.info(f"PSDIAG read  {name} @0x{addr:08x} -> 0x{v:08x}")
        except Exception as e:
            dut._log.info(f"PSDIAG read  {name} @0x{addr:08x} -> EXCEPTION {type(e).__name__}: {e}")

    # write/read-back on scratch SRAM: proves the port masters, not just reads
    try:
        await tb.c.write(0x2D002000, 0xA5A5BEEF)
        rb = await tb.c.read(0x2D002000)
        dut._log.info(f"PSDIAG wr/rd compute SRAM 0x2D002000 -> 0x{rb:08x} "
                      f"(expect 0xa5a5beef)")
    except Exception as e:
        dut._log.info(f"PSDIAG wr/rd EXCEPTION {type(e).__name__}: {e}")

    # and the role write specifically
    try:
        await tb.c.apb_write(0x2080, 0x03)
        await ClockCycles(dut.sys_fclk, 200)
        st = await tb.c.apb_read(0x2084)
        dut._log.info(f"PSDIAG after ROLE_CFG=0x03: ROLE_STATUS=0x{st:08x} "
                      f"role_locked_o={_i(dut.c_role_locked_o_0)}")
    except Exception as e:
        dut._log.info(f"PSDIAG role write EXCEPTION {type(e).__name__}: {e}")
    dut._log.info("PSDIAG COMPLETE")


@cocotb.test(timeout_time=40, timeout_unit="ms")
async def test_l0_sim_13_tx_aperture_faults_when_link_down(dut):
    """L0-SIM-13: a TX-aperture access with the link DOWN must ERROR, not hang.

    chiplet_d2d_decode.sv:116-131 states the contract: TideLink marks `ahb_tx_*`
    a WEDGE HAZARD — a write with the link down never completes and hangs the
    bus — so the decoder routes it to the default responder for a clean two-cycle
    AHB ERROR instead. "A stall is indistinguishable from a dead SoC on a bench,
    and a fault is a fact you can act on."

    This is the silicon wedge path, so the gate is worth an explicit test: if it
    regresses, the symptom on the bench is a board that has to be JTAG-POR'd, and
    nothing in the log says why.

    Deliberately runs BEFORE bring-up: link_active_i is low, tx_open is low.
    The cocotb timeout is the hang detector — if the access never completes this
    test fails by timing out, which is exactly the failure being guarded."""
    tb = ManualPair(dut)
    await tb.reset()

    assert not _i(dut.e_link_active_o), (
        "link reports active before bring-up — this test needs it DOWN to "
        "exercise the gate")

    tx_addr = 0x2E00_0100          # 0x2E, block 0 -> hsel_tx (the wedge aperture)
    resp = await tb.e.ahb.write(tx_addr, 0xDEADBEEF)
    raw = str(resp[0].get("resp", resp[0]))
    dut._log.info(f"TX-aperture write @0x{tx_addr:08x} with link DOWN -> resp={raw}")

    assert "ERROR" in raw.upper(), (
        f"TX-aperture write with the link down returned {raw!r}, expected an AHB "
        "ERROR. The wedge gate (chiplet_d2d_decode.sv:116-131) is not doing its "
        "job — on silicon this access hangs the bus and needs a JTAG POR.")

    # And the bus must still be usable afterwards: a fault, not a wedge.
    await tb.e.write(0x2D00_3000, 0x1234ABCD)
    rb = await tb.e.read(0x2D00_3000)
    assert rb == 0x1234ABCD, (
        f"the eth bus did not survive the faulted TX access: SRAM read back "
        f"0x{rb:08x}. The gate errored but left the bus unusable, which is the "
        "wedge it was meant to prevent.")
    dut._log.info("TX gate OK: clean ERROR, bus still usable afterwards")

    # MUTATION-TESTED. Retargeting this at tlapb ROLE_CFG (0x2E03_2080 —
    # writable, and demonstrably reachable pre-link since that is how the link
    # is brought up) returns AHBResp.OKAY and fails the assertion. So the check
    # discriminates by APERTURE and is not simply observing that everything
    # errors while the link is down.
    #
    # A first mutation attempt at 0x2E03_2108 (SWI_LANE_STATUS) was INCONCLUSIVE:
    # that register is read-only, so a write to it errors for its own reasons.
    # Recorded because it is an easy trap to repeat.


@cocotb.test(timeout_time=60, timeout_unit="ms")
async def test_l0_sim_17_far_die_dark_does_not_wedge_the_near_die(dut):
    """L0-SIM-17: with the far die held in reset, the near die must stay usable.

    The bench hazard this models is real and routine: one board is powered,
    reflashed or POR'd while the other is live. On silicon the near die's PS then
    issues D2D accesses into a die that cannot answer. If that hangs the near
    die's bus, the operator loses BOTH boards to one reset — and the eth-chiplet
    runbook already records a JTAG-POR-only recovery for exactly this class.

    Asserts the near die (E) survives with the far die (C) dark:
      * its own SRAM still reads and writes  (the SoC is alive)
      * the TX aperture faults rather than hanging (the wedge gate holds with no
        peer at all, not merely with a peer that is present but not linked)
      * the link never falsely reports up against a dead peer"""
    tb = ManualPair(dut)

    # Reset both, then hold die C in reset while die E runs.
    dut.e_sysresetn.value = 0
    dut.c_sysresetn.value = 0
    dut.e_pad_en.value = 1
    dut.c_pad_en.value = 1
    await ClockCycles(dut.sys_fclk, 20)
    dut.e_sysresetn.value = 1          # die E released
    # die C deliberately LEFT IN RESET — the far die is dark.
    await ClockCycles(dut.sys_fclk, 4000)

    assert not _i(dut.c_role_locked_o_0), "die C locked a role while held in reset"

    # 1. the near die's own SoC is alive
    await tb.e.write(0x2D00_4000, 0xFEEDFACE)
    rb = await tb.e.read(0x2D00_4000)
    assert rb == 0xFEEDFACE, (
        f"die E cannot use its OWN SRAM with the far die dark (read 0x{rb:08x}) "
        "— a dark peer has taken out the near die")

    # 2. its config plane still answers
    st = await tb.e.apb_read(0x2108)
    dut._log.info(f"die E SWI_LANE_STATUS with far die dark = 0x{st:08x}")

    # 3. the link must NOT claim to be up against a dead peer
    assert not _i(dut.e_link_active_o), (
        "die E reports link_active with the far die held in reset — a false "
        "link-up is worse than no link: bring-up would proceed onto a dead peer")

    # 4. the wedge gate holds with NO peer, not just an unlinked one
    resp = await tb.e.ahb.write(0x2E00_0100, 0xDEADBEEF)
    raw = str(resp[0].get("resp", resp[0]))
    assert "ERROR" in raw.upper(), (
        f"TX-aperture write with the far die DARK returned {raw!r}, expected "
        "ERROR — the gate protects against link-down but not against no-peer")

    # 5. and the near die is still usable after all of that
    await tb.e.write(0x2D00_4004, 0x5A5A5A5A)
    rb2 = await tb.e.read(0x2D00_4004)
    assert rb2 == 0x5A5A5A5A, (
        f"die E unusable after a faulted D2D access with the far die dark "
        f"(read 0x{rb2:08x})")
    dut._log.info("far-die-dark OK: die E fully usable, no false link-up, "
                  "TX gate held")


@cocotb.test(timeout_time=90, timeout_unit="ms")
async def test_diag_lone_d2d_write_delivers(dut):
    """DIAG: does a SINGLE isolated cross-die write deliver its data?

    docs/CHIPLET_ALIGNMENT_AUDIT.md reports that isolated D2D writes deliver
    0x00000000, citing the compute repo's own failing mailbox test. The
    strengthened L0-SIM-05 does NOT reproduce it — but there the doorbell
    follows a four-word burst, so it is not isolated in the sense that matters.

    This is the discriminating case: one write, nothing before it, into a
    freshly brought-up link. If the defect is real this is where it shows."""
    tb = ManualPair(dut)
    await tb.bring_up_manual()
    tb.assert_link_idle()
    cocotb.start_soon(tb.catch_compute_inbound())

    await tb.program_eth_cam(APERTURE_BYTE, C_INBOUND_MAILBOX, enable=True)
    # A LONE write to the doorbell offset. No data words, no warm-up.
    await tb.e.write((APERTURE_BYTE << 24) | IPC_SLOT0_CTRL, IPC_MSG_VALID)
    await ClockCycles(dut.sys_fclk, 4000)

    beats = tb.beats()
    dut._log.info(f"LONE-WRITE beats = {tb.fmt_beats()}")
    landed = {a & 0xFFFF: d for a, d, _e in beats}
    got = landed.get(IPC_SLOT0_CTRL)
    if got is None:
        dut._log.info("LONE-WRITE RESULT: the write never reached the inbound "
                      "port at all")
    elif got != IPC_MSG_VALID:
        dut._log.info(f"LONE-WRITE RESULT: DEFECT REPRODUCED — delivered "
                      f"0x{got:08x}, wrote 0x{IPC_MSG_VALID:08x}")
    else:
        dut._log.info(f"LONE-WRITE RESULT: delivered correctly (0x{got:08x}) — "
                      "the isolated-write defect does NOT reproduce on the het "
                      "pair in this posture")
