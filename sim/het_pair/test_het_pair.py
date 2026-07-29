"""Heterogeneous pair: an ethernet-chiplet die and a compute-chiplet die, one sim.

Copyright 2026, SoC Labs (www.soclabs.org)

Ported from `nanosoc-ethernet-chiplet/verif/g2_soc_pair/test_g2_soc_pair.py`,
which runs the same experiment between two IDENTICAL ethernet dies. Everything
that is only interesting because the dies DIFFER is new here.

WHAT THE PAIR LOOKS LIKE
    die E = nanosoc_eth_chiplet      (link master, role_strap_i   = 0)
    die C = nanosoc_compute_chiplet  (link slave,  role_strap_i_0 = 1)
    joined pad-to-pad, eth's single ribbon <-> compute's LINK 0 ribbon.

THE TWO THINGS THAT MAKE THIS HARDER THAN THE HOMOGENEOUS PAIR
------------------------------------------------------------------------------
1. ASYMMETRIC STIMULUS. The ethernet die exports `eth_ss_0_*`, an external AHB
   slave; every poke below goes through it. The compute die exports NO bus at
   all (81 ports, none of them AHB or APB — its only ingress is QSPI flash, UART
   and the SWJ-DP, and the SWJ-DP's `dap_m` is deliberately NOT granted the D2D
   windows). So the compute die cannot be poked at all.

   That rules out the APB bring-up recipe the homogeneous pair uses, which
   writes ROLE_CFG on BOTH dies. Instead this env brings the link up the way the
   silicon is meant to: STRAP + AUTONEGOTIATION, zero pokes on the compute side.
   That needs `NEGO_CFG_RESET = 7'h61` (nego_en | nego_force_lock |
   mask_hs_auto_en) instead of the `7'h00` both chiplet tops instantiate. It is
   applied as a PARAMETER at time 0 by a `defparam` in tb_het_pair.sv — see the
   comment there for why, and why that is a legitimate testbench action rather
   than a stub. `_check_autoneg_armed()` below only VERIFIES it took effect.

2. ASYMMETRIC ADDRESS MAP. The two dies do not agree on any of the three
   addresses this test cares about:

                          ethernet die        compute die
     d2d outbound window  0x2E00_0000         0x4000_0000  (link 0)
     peer aperture        0x2F......          0x41......
     TideLink cfg APB     0x2E03_0000         0x4003_0000
     inbound shared SRAM  0x2D......          0x2D......   <- the one agreement
     inbound IPC mailbox  0x23......          0x2A......   <- DIFFERENT

   The mailbox byte difference is the whole reason this file exists: on a
   homogeneous pair a CAM rule of 0x2F->0x23 lands in the far mailbox, and on
   THIS pair the identical rule must be REFUSED, because 0x23 is not in the
   compute die's inbound target set. `test_inbound_confinement_negative` is that
   case, and it is the single most valuable test here.
"""
import os

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge
from cocotb.utils import get_sim_time
from cocotbext.ahb import AHBBus, AHBLiteMaster

# The ethernet die's MAC wishbone slave drives X on hrdata during unrelated APB
# writes; ZEROS resolution matches g2_soc_pair and masks nothing asserted on here.
os.environ.setdefault("COCOTB_RESOLVE_X", "ZEROS")

# --- Ethernet die (die E) addresses ----------------------------------------
E_SHARED_SRAM   = 0x2D00_0000        # its own SRAM, via the eth_ss_0 passthrough
E_PEER_APERTURE = 0x2F00_0000        # its TideLink ahb_sub (address-translated)

# TideLink APB, reached through the ethernet chiplet decode's tlapb bridge. The
# bridge passes haddr[14:0] to the APB, so AHB 0x2E03_0000|off -> APB paddr off.
E_TLAPB_BASE             = 0x2E03_0000
APB_WL_LINK_ENABLE_RESET = 0x0208
APB_ROLE_CFG             = 0x2080    # NOT 0x2084 (TideLink REGISTER_MAP.md:164)
APB_R8_SLOT0             = 0x2100
APB_R8_SWI_LANE_STATUS   = 0x2108    # bit[16] = cal_done
CAM_BASE_OFFSET          = 0x4000
CAM_CTRL                 = 0x4004    # bit[0] = global_enable
CAM_RULE_0               = 0x4010    # [0]=en [15:8]=match [23:16]=replace

# --- Compute die (die C) addresses — NEW with G2 -----------------------------
# The compute top now exports `ps_ahb_s`, an AHB slave that the system
# description turns into top-matrix initiator `ps_m` "reaching the whole compute
# map" (nanosoc_compute_soc.yaml:104). So die C is pokeable, exactly as die E is
# through eth_ss_0, and the TB no longer has to deposit its role bits.
#
# Window geometry is now a PARAMETER, not a hard-wired assumption (G4):
# chiplet_d2d_decode is instantiated .WINDOW_BASE(32'h4000_0000) for link 0
# (nanosoc_compute_chiplet.sv:525-526). Its region map (chiplet_d2d_decode.sv:49-67)
# gives, with B = WINDOW_BASE top byte:
#     peer   = (B+1)<<24                -> 0x4100_0000   <-- 0x41, NOT 0x40
#     tlapb  =  B<<24 + 0x30000         -> 0x4003_0000
# The long-open "0x40 or 0x41?" question is settled in RTL by that parameter.
C_TLAPB_BASE    = 0x4003_0000        # compute link-0 TideLink APB
C_PEER_APERTURE = 0x4100_0000        # compute link-0 peer aperture
C_LINK1_PEER    = 0x6100_0000        # link 1 — uncabled on this bench

ROLE_CFG_SLAVE_LOCK = 0x03           # bit[0]=1 slave, bit[1]=1 lock (W1S)

# --- The compute die's inbound target set (nanosoc_compute_soc.yaml:1082-1100)
# Only these two byte values are decoded by the compute SoC's D2D0_M matrix
# port. Everything else goes to its default slave and takes a two-cycle ERROR.
C_INBOUND_SRAM    = 0x2D
C_INBOUND_MAILBOX = 0x2A
# The ETHERNET die's mailbox byte. Valid on an eth<->eth pair; NOT a compute
# inbound target. The negative test relies on exactly this divergence.
E_INBOUND_MAILBOX = 0x23

# The ethernet die's peer window is all of 0x2F (chiplet_d2d_decode: a_peer =
# haddr[24]). It is a SINGLE 16 MB aperture, and the CAM rewrites whole bytes
# ([15:8]=match, [23:16]=replace), so the die can reach exactly ONE remote 16 MB
# target at a time. Reaching both the far SRAM and the far mailbox means
# reprogramming RULE_0 between them — which the mailbox test does.
APERTURE_BYTE = 0x2F

PAYLOAD = 0xC0FFEE01

# 3-write LL bootstrap; order matters (swreset first clears CR/CRACK sticky).
LL_SWRESET_ON  = 0x00027F08
LL_SWRESET_OFF = 0x00027F00
LL_ENABLE      = 0x00027F07

ROLE_CFG_MASTER_LOCK = 0x02

# NEGO_CFG bits: [0] nego_en, [5] nego_force_lock, [6] mask_hs_auto_en.
NEGO_CFG_AUTONOMOUS = 0x61


def _cam_rule(match_byte, replace_byte):
    return (replace_byte << 16) | (match_byte << 8) | 1


def _rd(resp):
    """cocotbext-ahb returns [{'resp':…, 'data':'0x…'}]."""
    return int(resp[0]["data"], 16)


def _i(sig, default=0):
    """int() of a signal, tolerating X/Z.

    Bring-up polls run while parts of both dies are still resolving, so a raw
    int(sig.value) raises ValueError on an X and aborts the poll rather than
    simply not-yet-satisfying it. Unresolvable reads back as `default`.
    """
    try:
        v = sig.value
        return int(v) if v.is_resolvable else default
    except (ValueError, TypeError, AttributeError):
        return default


async def _heartbeat(dut, every=2000):
    """A frozen heartbeat while the process burns CPU means a zero-delay loop,
    which sim-time timeouts cannot catch."""
    while True:
        await ClockCycles(dut.sys_fclk, every)
        dut._log.info(f"heartbeat: t={get_sim_time('us'):.1f} us")


class EthDie:
    """The ethernet die. Pokeable: an AHB master on its eth_ss_0 port."""

    def __init__(self, dut):
        self.dut = dut
        self.log = dut._log
        bus = AHBBus.from_prefix(dut, "e_eth_ss_0")
        self.ahb = AHBLiteMaster(bus, dut.sys_fclk, dut.e_sysresetn, timeout=50000)

    async def write(self, addr, data):
        await self.ahb.write(addr, data)

    async def read(self, addr):
        return _rd(await self.ahb.read(addr))

    async def apb_write(self, off, data):
        await self.write(E_TLAPB_BASE + off, data)

    async def apb_read(self, off):
        return await self.read(E_TLAPB_BASE + off)


class ComputeDie:
    """The compute die. Pokeable since G2: an AHB master on its `ps_ahb_s` port.

    Deliberately mirrors EthDie so the two dies are driven the same way. Its
    apb_write/apb_read target the COMPUTE tlapb base (0x4003_0000), not the
    ethernet one — the single most likely copy-paste error on this pair.
    """

    def __init__(self, dut):
        self.dut = dut
        self.log = dut._log
        bus = AHBBus.from_prefix(dut, "c_ps_ahb_s")
        self.ahb = AHBLiteMaster(bus, dut.sys_fclk, dut.c_sysresetn, timeout=50000)

    async def write(self, addr, data):
        await self.ahb.write(addr, data)

    async def read(self, addr):
        return _rd(await self.ahb.read(addr))

    async def apb_write(self, off, data):
        await self.write(C_TLAPB_BASE + off, data)

    async def apb_read(self, off):
        return await self.read(C_TLAPB_BASE + off)


class Pair:
    def __init__(self, dut):
        self.dut = dut
        self.log = dut._log
        self.e = EthDie(dut)
        self.c = ComputeDie(dut)
        cocotb.start_soon(_heartbeat(dut))

    # ------------------------------------------------------------------
    # Sim-only calibrator bypass. Without it both calibrators sit in
    # S_VALIDATE for ~2M link cycles and cal_done never asserts in any sane
    # sim budget. This is the same hook g2_soc_pair uses; it is a TB-only
    # early-exit built into the calibrator FOR this purpose.
    # ------------------------------------------------------------------
    def _calibrator_sim_bypass(self):
        targets = [
            ("die E", self.dut.u_dieE.u_tidelink),
            ("die C link0", self.dut.u_dieC.u_tidelink_0),
        ]
        for name, tl in targets:
            try:
                tl.u_chiplet_controller.u_calibrator.tb_early_exit_force_q.value = 1
            except AttributeError:
                self.log.warning(f"{name}: tb_early_exit_force_q missing — bypass NOT applied")

    # ------------------------------------------------------------------
    # Arm autonomous role negotiation on BOTH dies.
    #
    # WHY THIS IS NOT A STUB. `nego_cfg_reg` is a real configuration register
    # whose POR value is the PARAMETER `NEGO_CFG_RESET` (tidelink_top.sv:123).
    # Both chiplet tops instantiate `tidelink_top` without overriding it, so it
    # PORs to 7'h00 => nego_en=0 => the autoneg FSM parks in ST_BYPASS and
    # `role_lock_reg` is never set => the Wlink is held in reset forever
    # (axi_chiplet_controller.sv:2832) => link_active can never assert.
    #
    # The homogeneous pair works around that by writing ROLE_CFG over APB on
    # BOTH dies. That route does not exist here: the compute die has no bus.
    #
    # So we set the register to the value the ASIC build already defaults to —
    # `tidelink_dft_wrapper.sv:137` has `NEGO_CFG_RESET = 7'h61` — i.e. the
    # posture the chiplets are INTENDED to tape out in. TideLink's own
    # `cocotb/tidelink_top_pair/test_zeropoke_por.py` proves a pair reaches
    # bilateral cal=S_DONE and fcsm=4 from this value with zero pokes.
    #
    # This is a configuration choice a chiplet integrator must make at
    # instantiation, and the finding that BOTH tops currently make it wrong for
    # a firmware-free die is recorded in docs/SIM_PLAN.md as a required RTL
    # change. It is applied here so the rest of the pair can be exercised; it
    # does not weaken any assertion below.
    # ------------------------------------------------------------------
    def _check_autoneg_armed(self):
        """Verify NEGO_CFG_RESET reached both controllers.

        The value is applied as a PARAMETER at time 0 by `defparam` in
        tb_het_pair.sv — see the long comment there for why a post-reset
        register write is NOT sufficient (it gets as far as role-lock and
        cal_done, then the FCSM sticks at state 1 because the link-enable
        sequencing was already decided from the POR value).

        This only READS the register, so it cannot mask a mis-configured build:
        if the defparam is absent the assertion below fails loudly rather than
        the test silently limping to a confusing FCSM timeout.
        """
        for name, tl in (("die E", self.dut.u_dieE.u_tidelink),
                         ("die C link0", self.dut.u_dieC.u_tidelink_0)):
            got = _i(tl.u_chiplet_controller.nego_cfg_reg, -1)
            self.log.info(f"{name}: nego_cfg_reg = 0x{got:02x} "
                          f"(expect 0x{NEGO_CFG_AUTONOMOUS:02x})")
            assert got == NEGO_CFG_AUTONOMOUS, (
                f"{name}: nego_cfg_reg is 0x{got:02x}, expected "
                f"0x{NEGO_CFG_AUTONOMOUS:02x}. The tb_het_pair.sv defparam did "
                f"not take effect, so autonomous negotiation is not armed and "
                f"this die cannot lock its role. See docs/SIM_PLAN.md F3.")

    async def reset(self):
        self.dut.e_sysresetn.value = 0
        self.dut.c_sysresetn.value = 0
        self.dut.e_pad_en.value = 1
        self.dut.c_pad_en.value = 1
        await ClockCycles(self.dut.sys_fclk, 20)
        self._calibrator_sim_bypass()
        self.dut.e_sysresetn.value = 1
        self.dut.c_sysresetn.value = 1
        # Both SoCs' reset controllers lift internal resets; each boot core
        # reaches its flash-magic halt on the unprogrammed flash, so both buses
        # go quiet. The compute die is never touched again after this point.
        await ClockCycles(self.dut.sys_fclk, 200)
        # Re-apply: the first application happens before each SoC's PRMU has
        # released poresetn, and the calibrator's reset is ~poresetn, so it can
        # be cleared. Idempotent.
        self._calibrator_sim_bypass()
        self._check_autoneg_armed()
        await ClockCycles(self.dut.sys_fclk, 4000)

    # --- link bring-up -------------------------------------------------
    async def wait_role_locked(self, cycles=2000):
        """Both dies must latch a role. On this pair the compute die can ONLY
        get there through autonegotiation — nothing can write its ROLE_CFG.

        Budget is deliberately generous (2000 * 50 = 100k cycles = 2 ms). The two
        dies do NOT resolve together: measured on this pair the SLAVE (compute)
        loses at ~65 us but the MASTER (ethernet) does not win until ~260 us. A
        budget sized for the slave alone times out on a link that is in fact
        converging, so anything on-bench that polls role_locked needs the same
        margin — see docs/SIM_PLAN.md 8a."""
        for _ in range(cycles):
            if _i(self.dut.e_role_locked_o) and _i(self.dut.c_role_locked_o_0):
                return
            await ClockCycles(self.dut.sys_fclk, 50)
        raise TimeoutError(
            "role_locked never asserted on both dies. die E="
            f"{self.dut.e_role_locked_o.value} die C link0={self.dut.c_role_locked_o_0.value}. "
            "The compute die has no APB path, so if it is the one stuck, autonegotiation "
            "did not resolve — see docs/SIM_PLAN.md 'strap-only bring-up'.")

    async def wait_cal_done(self, max_cycles=500_000):
        """cal_done is only READABLE on the ethernet die (APB). For the compute
        die we read the calibrator state hierarchically — there is no bus.

        Budget matches the proven upstream helper
        (tidelink/cocotb/tidelink_top_pair_v2/pair_v2_common.py:252,
        max_cycles=500000). Convergence here is SLOWER than the raw TideLink
        pair: each die is a whole SoC, and on this pair the master's role does
        not resolve until ~260 us. A budget sized for the raw pair times out
        before the calibrators have had a chance."""
        for _ in range(max_cycles // 200):
            e = await self.e.apb_read(APB_R8_SWI_LANE_STATUS)
            c_done = _i(self.dut.u_dieC.u_tidelink_0.u_chiplet_controller
                        .u_calibrator.calibration_done)
            if ((e >> 16) & 1) and c_done:
                return
            await ClockCycles(self.dut.sys_fclk, 200)
        raise TimeoutError(
            "cal_done never asserted on both dies within budget. "
            f"eth R8_SWI_LANE_STATUS=0x{e:08x} (bit16=cal_done), "
            f"compute calibration_done={c_done}, "
            f"compute cal cur_state="
            f"{_i(self.dut.u_dieC.u_tidelink_0.u_chiplet_controller.u_calibrator.cur_state, -1)}")

    async def to_data_mode(self):
        """LL bootstrap. Issued on the ethernet die only.

        On the compute die these registers are ALREADY at the value this
        sequence restores: LL_ENABLE (0x00027F07) is bit-for-bit the POR reset
        of {preq_data_id=0x02, short_packet_max=0x7F, enable|lltx|llrx=1}
        (Wlink.v:2513-2589). So a cold-POR die needs no LL bootstrap, which is
        the only reason this pair can come up at all.
        """
        await self.e.apb_write(APB_R8_SLOT0, 0)
        await ClockCycles(self.dut.sys_fclk, 20)
        for val in (LL_SWRESET_ON, LL_SWRESET_OFF, LL_ENABLE):
            await self.e.apb_write(APB_WL_LINK_ENABLE_RESET, val)
            await ClockCycles(self.dut.sys_fclk, 20)
        await ClockCycles(self.dut.sys_fclk, 5000)   # CR/CRACK exchange

    async def bring_up(self, ll_bootstrap=False):
        """Reset -> role lock -> cal_done. NO LL bootstrap by default.

        `to_data_mode()` is deliberately OFF. In the autonomous-negotiation mode
        this pair must use (see tb_het_pair.sv's defparam), the LL bootstrap is both
        unnecessary and actively harmful:

          * Unnecessary — `LL_ENABLE` (0x00027F07) is bit-for-bit the POR reset
            value of the fields it writes (Wlink.v:2513,2519,2570,2577,2583,2589),
            and `R8_SLOT0 = 0` is already the POR value
            (axi_chiplet_controller.sv:2015-2043). From a cold POR both writes
            restore what is already there.
          * Harmful — MEASURED on this pair: with autonegotiation armed, the
            first AHB write of the sequence (R8_SLOT0 @ 0x2E03_2100) never
            completes; the cocotbext-ahb master times out after 50000 cycles at
            ~3.48 ms sim, while the ethernet die's autoneg FSM is still
            advancing (states 11->12->13->14 observed between 1.67 and 2.40 ms).
            Reads of the same register file during `wait_cal_done` succeed, so
            the bus is healthy — it is specifically an external WRITE landing
            while the FSM owns the register file.

        That combination — a manual bring-up recipe that hangs the bus when
        autonomous negotiation is also enabled — is recorded as a finding in
        docs/SIM_PLAN.md. Set `ll_bootstrap=True` to reproduce it.
        """
        await self.reset()
        await self.wait_role_locked()
        await self.wait_cal_done()
        if ll_bootstrap:
            await self.to_data_mode()
        else:
            # Let the FSM finish its own sequencing before the data plane is used.
            await ClockCycles(self.dut.sys_fclk, 5000)

    # --- observation ---------------------------------------------------
    def link_carries_m2s(self):
        """The compute die has seen the ethernet die's CR and CRACK packets —
        evidence the M->S link layer is live. `link_active` alone is just
        role_locked_o."""
        f = (self.dut.u_dieC.u_tidelink_0.u_chiplet_controller
             .u_wlink.tl2wl.wlink_tidelinktl)
        return _i(f.cr_pkt_seen_rx) and _i(f.crack_pkt_seen_rx)

    def fcsm_state(self, die):
        tl = (self.dut.u_dieE.u_tidelink if die == "e"
              else self.dut.u_dieC.u_tidelink_0)
        # Signal names per tidelink/cocotb/tidelink_top_pair_v2/pair_v2_common.py:221
        return _i(tl.u_chiplet_controller.u_wlink.tl2wl.wlink_tidelinktl.state, -1)

    def observe_compute_inbound(self):
        """The address the compute die's inbound D2D port (its link-0 ahb_mng)
        is presenting. Read hierarchically so the CAM's translated byte is
        visible even before it retires into SRAM — and because there is no bus
        on that die to read it any other way."""
        return _i(self.dut.u_dieC.d2d0_ahb_s_haddr, -1)

    async def program_eth_cam(self, match_byte, replace_byte, enable=True):
        """Program the ETHERNET die's egress CAM. CTRL armed last so a
        half-configured rule is never live."""
        await self.e.apb_write(CAM_BASE_OFFSET, 0x00000000)
        await self.e.apb_write(CAM_RULE_0, _cam_rule(match_byte, replace_byte))
        await self.e.apb_write(CAM_CTRL, 1 if enable else 0)

    async def catch_compute_inbound(self):
        """Record every write beat the compute die's inbound D2D port retires:
        address in its address phase (htrans[1] & hwrite & hready), data and
        response on the completing cycle. This is how we tell 'the far SoC
        accepted it' from 'the far SoC erred' — the confinement test's evidence."""
        s = self.dut.u_dieC
        self.inbound = []
        pend = None
        while True:
            await RisingEdge(self.dut.sys_fclk)

            def rd(sig):
                v = getattr(s, sig).value
                return int(v) if v.is_resolvable else None

            htrans = rd("d2d0_ahb_s_htrans")
            hready = rd("d2d0_ahb_s_hready")
            hwrite = rd("d2d0_ahb_s_hwrite")
            haddr = rd("d2d0_ahb_s_haddr")
            if pend is not None and hready:
                self.inbound.append((pend, rd("d2d0_ahb_s_hwdata"),
                                     rd("d2d0_ahb_s_hresp")))
                pend = None
            if htrans and (htrans & 0b10) and hwrite and hready:
                pend = haddr

    def beats(self):
        """Recorded inbound beats, safe before the catcher coroutine first runs."""
        return getattr(self, "inbound", [])

    def fmt_beats(self):
        return [(hex(a) if a is not None else None,
                 hex(d) if d is not None else None, r) for a, d, r in self.beats()]

    def saw_error_response(self):
        return any(r == 1 for (_a, _d, r) in self.beats())

    def landed_addresses(self):
        return [a for (a, _d, r) in self.beats() if r != 1]


# ===========================================================================
# Fast harness smoke test — no link. Proves the ethernet die's stimulus port
# reaches its own SRAM, and that the compute die boots and stays quiet.
# ===========================================================================
@cocotb.test(timeout_time=3, timeout_unit="ms")
async def test_smoke_harness(dut):
    tb = Pair(dut)
    await tb.reset()

    await tb.e.write(E_SHARED_SRAM + 0x40, 0xA5A50001)
    got = await tb.e.read(E_SHARED_SRAM + 0x40)
    assert got == 0xA5A50001, f"die E shared SRAM read-back 0x{got:08x} != 0xA5A50001"

    # The compute die must have come out of reset and released its AHB reset.
    assert _i(dut.c_sys_hresetn) == 1, (
        "compute die never released sys_hresetn — its PRMU did not bring up the "
        "AHB clock/reset, so nothing downstream can work")

    # Its uncabled link 1 must NOT be up: nothing is driving its RX pads.
    assert _i(dut.c_link_active_o_1) == 0, (
        "compute die link 1 reports active with NOTHING connected to its pads — "
        "a link that trains against an open ribbon would be a real bug")

    dut._log.info("SMOKE ok: die E stimulus reaches its SRAM; die C booted; link 1 down")


# ===========================================================================
# Link bring-up across two DIFFERENT designs, with pokes on one side only.
# ===========================================================================
@cocotb.test(timeout_time=60, timeout_unit="ms")
async def test_het_link_brings_up(dut):
    tb = Pair(dut)
    await tb.bring_up()

    assert _i(dut.e_link_active_o) == 1, "ethernet die link_active never asserted"
    assert _i(dut.c_link_active_o_0) == 1, "compute die link 0 link_active never asserted"

    # Role must have resolved OPPOSITELY across two different designs. The straps
    # say eth=master, compute=slave; autonegotiation must honour that.
    e_master = _i(dut.e_role_is_master_o)
    c_master = _i(dut.c_role_is_master_o_0)
    assert e_master == 1 and c_master == 0, (
        f"role negotiation resolved wrongly: eth die master={e_master}, "
        f"compute die master={c_master}. Expected eth=1 (role_strap_i=0) and "
        f"compute=0 (role_strap_i_0=1).")

    e_fcsm, c_fcsm = tb.fcsm_state("e"), tb.fcsm_state("c")
    assert e_fcsm == 4 and c_fcsm == 4, (
        f"FCSM did not reach state 4 on both dies (eth={e_fcsm}, compute={c_fcsm})")

    assert tb.link_carries_m2s(), (
        "M->S link layer never came up (cr/crack not seen on the compute die)")

    # The uncabled link must still be down after the cabled one trained.
    assert _i(dut.c_link_active_o_1) == 0, (
        "compute die link 1 came up while only link 0 is cabled")

    dut._log.info("LINK ok: heterogeneous pair at FCSM=4 both dies, roles resolved "
                  "eth=master / compute=slave, link 1 correctly down")


# ===========================================================================
# The data plane: an ethernet-die peer write lands in the COMPUTE die's real
# shared_sram_0, and reads back over the link.
# ===========================================================================
@cocotb.test(timeout_time=90, timeout_unit="ms")
async def test_peer_write_eth_to_compute(dut):
    tb = Pair(dut)
    await tb.bring_up()
    assert tb.link_carries_m2s(), "link layer never came up; cannot test the data plane"

    cocotb.start_soon(tb.catch_compute_inbound())

    # 0x2F -> 0x2D. 0x2D is shared_sram_0 on BOTH designs — the one address both
    # dies agree on, and therefore the only one that works with no re-mapping.
    await tb.program_eth_cam(APERTURE_BYTE, C_INBOUND_SRAM, enable=True)

    peer_addr = (APERTURE_BYTE << 24) | 0x001000
    await tb.e.write(peer_addr, PAYLOAD)
    await ClockCycles(dut.sys_fclk, 4000)

    inbound = tb.observe_compute_inbound()
    dut._log.info(f"compute inbound beats = {tb.fmt_beats()}")
    assert (inbound >> 24) == C_INBOUND_SRAM, (
        f"compute die inbound saw 0x{inbound:08x}; expected upper byte "
        f"0x{C_INBOUND_SRAM:02x} (the ethernet die's CAM should have rewritten "
        f"0x{APERTURE_BYTE:02x}->0x{C_INBOUND_SRAM:02x})")

    assert not tb.saw_error_response(), (
        "the compute die ERRORed a write to 0x2D — that byte IS in its inbound "
        f"target set, so this is a real fault. beats={tb.fmt_beats()}")

    # Read the payload back ACROSS THE LINK out of the compute die's SRAM. There
    # is no bus on that die, so this round-trip is the only way to prove the data
    # (not just the address) landed — and it exercises the return path too.
    rb = await tb.e.read(peer_addr)
    assert rb == PAYLOAD, (
        f"peer read-back 0x{peer_addr:08x} returned 0x{rb:08x}, expected "
        f"0x{PAYLOAD:08x} — the write reached the compute die's inbound port but "
        f"the data did not survive the round trip.")

    dut._log.info(f"DATA ok: die E 0x{peer_addr:08x} -> die C shared_sram_0 "
                  f"0x{C_INBOUND_SRAM:02x}001000, read back 0x{rb:08x}")


# ===========================================================================
# Multi-word sequence — catches off-by-one BETWEEN beats that a lone access
# cannot (a write-data delay mis-aligning beat N with N+1, or a read pipe-offset
# that fails to re-arm). This is what a memcpy across the aperture does.
# ===========================================================================
@cocotb.test(timeout_time=90, timeout_unit="ms")
async def test_peer_sequence_eth_to_compute(dut):
    tb = Pair(dut)
    await tb.bring_up()
    assert tb.link_carries_m2s(), "link layer never came up"

    await tb.program_eth_cam(APERTURE_BYTE, C_INBOUND_SRAM, enable=True)

    base = (APERTURE_BYTE << 24) | 0x002000
    seq = [(base + 4 * i, 0x5EED0000 + (i << 4) + i) for i in range(8)]
    for addr, val in seq:
        await tb.e.write(addr, val)
    await ClockCycles(dut.sys_fclk, 4000)
    for addr, val in seq:
        rb = await tb.e.read(addr)
        assert rb == val, (
            f"sequence mismatch at 0x{addr:08x}: read 0x{rb:08x}, wrote 0x{val:08x} "
            f"— a consecutive-beat write or read corrupted the sequence")

    dut._log.info(f"SEQ ok: {len(seq)} words across the heterogeneous aperture intact")


# ===========================================================================
# The IPC mailbox path. On an eth<->eth pair the far mailbox is at 0x23. On THIS
# pair it is at 0x2A. Same aperture, different rule — this test is the one that
# proves the map difference is handled rather than assumed away.
# ===========================================================================
@cocotb.test(timeout_time=90, timeout_unit="ms")
async def test_ipc_mailbox_eth_to_compute(dut):
    tb = Pair(dut)
    await tb.bring_up()
    assert tb.link_carries_m2s(), "link layer never came up"

    cocotb.start_soon(tb.catch_compute_inbound())

    # 0x2F -> 0x2A, NOT 0x23. The ethernet die's own mailbox byte would be wrong.
    await tb.program_eth_cam(APERTURE_BYTE, C_INBOUND_MAILBOX, enable=True)

    mbox_addr = (APERTURE_BYTE << 24) | 0x000010
    await tb.e.write(mbox_addr, 0xBEEF0001)
    await ClockCycles(dut.sys_fclk, 4000)

    inbound = tb.observe_compute_inbound()
    assert (inbound >> 24) == C_INBOUND_MAILBOX, (
        f"compute die inbound saw 0x{inbound:08x}; expected upper byte "
        f"0x{C_INBOUND_MAILBOX:02x} (its IPC mailbox). Note the ethernet die's "
        f"own mailbox is at 0x{E_INBOUND_MAILBOX:02x} — the two designs differ, "
        f"and a rule copied from the homogeneous pair would land nowhere.")
    assert not tb.saw_error_response(), (
        f"the compute die ERRORed a write to its own mailbox byte "
        f"0x{C_INBOUND_MAILBOX:02x}. beats={tb.fmt_beats()}")

    dut._log.info(f"MAILBOX ok: die E -> die C ipc_mailbox_0 at "
                  f"0x{C_INBOUND_MAILBOX:02x}000010")


# ===========================================================================
# Control case: CAM off => the address arrives UNtranslated. Without this,
# nothing above proves the 0x2D/0x2A upper bytes came from the CAM rather than
# from an identity passthrough.
# ===========================================================================
@cocotb.test(timeout_time=90, timeout_unit="ms")
async def test_cam_disabled_is_identity(dut):
    tb = Pair(dut)
    await tb.bring_up()
    assert tb.link_carries_m2s(), "link layer never came up"

    await tb.program_eth_cam(APERTURE_BYTE, C_INBOUND_SRAM, enable=False)

    peer_addr = (APERTURE_BYTE << 24) | 0x003000
    await tb.e.write(peer_addr, PAYLOAD ^ 0xFFFF)
    await ClockCycles(dut.sys_fclk, 4000)

    inbound = tb.observe_compute_inbound()
    assert (inbound >> 24) == APERTURE_BYTE, (
        f"with the CAM disabled the compute die inbound saw 0x{inbound:08x}; "
        f"expected an identity map (upper byte 0x{APERTURE_BYTE:02x}). The "
        f"translated byte in the other tests did not come from the CAM.")

    dut._log.info(f"CONTROL ok: CAM disabled -> compute inbound 0x{inbound:08x} (identity)")


# ===========================================================================
# NEGATIVE: inbound confinement across DIFFERENT designs.
#
# This is the test that only a heterogeneous pair can run, and the one most
# likely to catch a real bring-up bug.
#
# 0x23 is the ETHERNET SoC's ipc_mailbox_0. On a homogeneous eth pair, a CAM
# rule of 0x2F->0x23 lands in the far die's mailbox and is entirely legitimate —
# so this exact rule is a plausible copy-paste from the existing bring-up
# scripts. On THIS pair it must be REFUSED: the compute SoC's inbound matrix
# port decodes only 0x2A and 0x2D and sends everything else to its default
# slave, which raises a two-cycle AHB ERROR.
#
# A silent OKAY here would mean a cross-die write can reach an unintended target
# on the compute die. That is a security-relevant confinement failure, and it is
# invisible to both repos' own testbenches.
# ===========================================================================
@cocotb.test(timeout_time=90, timeout_unit="ms")
async def test_inbound_confinement_negative(dut):
    tb = Pair(dut)
    await tb.bring_up()
    assert tb.link_carries_m2s(), "link layer never came up"

    cocotb.start_soon(tb.catch_compute_inbound())

    # The rule a homogeneous-pair script would use — wrong for this pair.
    await tb.program_eth_cam(APERTURE_BYTE, E_INBOUND_MAILBOX, enable=True)

    bad_addr = (APERTURE_BYTE << 24) | 0x000020
    await tb.e.write(bad_addr, 0xDEADBEEF)
    await ClockCycles(dut.sys_fclk, 4000)

    inbound = tb.observe_compute_inbound()
    dut._log.info(f"confinement: compute inbound haddr=0x{inbound:08x} "
                  f"beats={tb.fmt_beats()}")

    # The translated address must have arrived (proving the write really did
    # cross), and the compute die must have REFUSED it.
    assert (inbound >> 24) == E_INBOUND_MAILBOX, (
        f"compute inbound saw 0x{inbound:08x}; expected the CAM-translated "
        f"0x{E_INBOUND_MAILBOX:02x}...... — if the address never arrived, this "
        f"test proves nothing about confinement")

    assert tb.saw_error_response(), (
        f"the compute die ACCEPTED a cross-die write to 0x{E_INBOUND_MAILBOX:02x}"
        f"000020 with OKAY. That byte is the ETHERNET SoC's ipc_mailbox_0 and is "
        f"NOT in the compute SoC's inbound target set (only 0x2A and 0x2D are). "
        f"It must take a two-cycle AHB ERROR from the matrix default slave. "
        f"A cross-die write reaching an unintended target is a confinement "
        f"failure. beats={tb.fmt_beats()}")

    assert E_INBOUND_MAILBOX not in [a >> 24 for a in tb.landed_addresses()], (
        f"a write to the excluded byte 0x{E_INBOUND_MAILBOX:02x} LANDED on the "
        f"compute die despite the error response")

    dut._log.info(f"CONFINEMENT ok: 0x{E_INBOUND_MAILBOX:02x} (eth's mailbox byte) "
                  f"correctly DECERRed by the compute die, which maps its mailbox "
                  f"at 0x{C_INBOUND_MAILBOX:02x}")
