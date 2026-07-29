# Pre-silicon simulation plan — the heterogeneous pair

**Scope.** One real `nanosoc_eth_chiplet` die and one real
`nanosoc_compute_chiplet` die in a single simulation, D2D pins tied together,
driven by cocotb. This is the cheapest gate before two KR260 boards and a J21
ribbon are involved.

**Status.** The pair **elaborates and partially runs**. `sim/het_pair` builds a `simv`
containing both chiplet tops — 302 modules, zero errors, VCS 2022.06-SP2 — the
harness smoke test passes, and the two dies **negotiate opposite link roles and
reach `cal_done` from their straps alone, with zero pokes on the compute die.**
Getting there required a build change (a Verilog library partition) and surfaced
six findings that block or constrain the bench. Three need an RTL or firmware
change in a chiplet repo, and one (F6, the Wlink FCSM stalling at state 1) is
still open and is what currently stops the data-plane tests.

Everything below cites `file:line`. Paths are relative to the two chiplet
checkouts unless absolute.

---

## 1. Verdict

**Can the two tops be tied together? Yes.** The D2D pins are compatible 1:1 with
no exceptions, the pair co-elaborates once the build partitions the two SoCs'
module namespaces, and the link negotiates and calibrates across the two
different designs. **But three RTL/firmware changes are needed before the
heterogeneous pair is genuinely usable, and one testbench-side change (F1) was
required just to build it at all.** None of the six findings below is visible to
either repo's own homogeneous pair — which is the argument for keeping this
bench.

| # | Finding | Severity | Fix belongs in |
|---|---|---|---|
| **F1** | The two generated SoCs share **39 module names with different content**, including `PHC_AHB` (different port lists) and `nanosoc_ss_cpu_plus` (disagrees on `system_hreadyout`). A single-namespace compile is order-dependent and wrong. | **Blocker (solved here)** | Testbench/build — **done**, `sim/het_pair/gen_libmap.py` |
| **F2** | `nanosoc_compute_chiplet` exports **no AHB/APB port**. Its TideLink config APB is unreachable, so its CAM cannot be programmed and the compute→eth direction cannot be tested. | **Blocker for half the data plane** | Compute chiplet RTL |
| **F3** | Both chiplet tops instantiate `tidelink_top` with the default `NEGO_CFG_RESET = 7'h00`, so a die **cannot lock its role without APB writes**. Combined with F2, the pair cannot come up at all as shipped. | **Blocker (worked around in TB)** | Both chiplet RTL (one parameter) |
| **F4** | The compute chiplet's peer aperture is at `0x41......`, but its firmware targets `0x4000_0100`, which the shared decoder resolves as the **TX aperture**. Silent wrong-destination write once the link is up. | **Correctness defect** | Compute chiplet firmware or decoder |
| **F5** | With autonegotiation armed, the legacy manual **LL bootstrap hangs the AHB matrix**: an external APB *write* never completes while the negotiation FSM owns the register file (reads are fine). The writes are also no-ops from cold POR. | **Bring-up hazard** | Host bring-up code / documented rule |
| **F6** | The pair reaches role-lock and `cal_done` but the Wlink **FCSM stalls at state 1** and never reaches 4, so the data plane never opens. Two hypotheses tested and eliminated. **Unresolved — this is the current blocker.** | **Blocker, open** | TideLink integration (see §8a) |

---

## 2. D2D pin lists, side by side

Extracted mechanically from
`nanosoc-ethernet-chiplet/src/rtl/nanosoc_eth_chiplet.sv:39-208` (111 ports) and
`NanoSoC-Compute-Chiplet/src/rtl/nanosoc_compute_chiplet.sv:62-189` (81 ports).

**Result: every ethernet D2D pin has an exact compute counterpart on link 0.
Same direction, same width, no exceptions.** The compute chiplet simply carries
two of each set, suffixed `_0` / `_1`.

| Ethernet (`nanosoc_eth_chiplet.sv`) | Compute link 0 | Dir | Width |
|---|---|---|---|
| `pad_clk_tx` :146 | `pad_clk_tx_0` :123 | out | 1 |
| `pad_tx` :147 | `pad_tx_0` :124 | out | `NUM_PHY_LANES` |
| `pad_clk_rx` :148 | `pad_clk_rx_0` :125 | in | 1 |
| `pad_rx` :149 | `pad_rx_0` :126 | in | `NUM_PHY_LANES` |
| `user_ref_clk` :150 | `user_ref_clk_0` :127 | in | 1 |
| `idelay_ref_clk` :151 | `idelay_ref_clk_0` :128 | in | 1 |
| `i2c_scl_i/o/t`, `i2c_sda_i/o/t` :156-161 | `…_0` :130-135 | mixed | 1 each |
| `role_strap_i` :162 | `role_strap_i_0` :137 | in | 1 |
| `mask_hs_bypass_i` :178 | `mask_hs_bypass_i_0` :138 | in | 1 |
| `apb_debug_unlock_i` :179 | `apb_debug_unlock_i_0` :139 | in | 1 |
| `nego_priority_i` :177 | `nego_priority_i_0` :141 | in | 16 |
| `puf_seed` :180 | `puf_seed_0` :142 | in | 16 |
| `puf_ready` :181 | `puf_ready_0` :143 | in | 1 |
| `link_active_o` :201 | `link_active_o_0` :145 | out | 1 |
| `d2d_reset_o` :202 | `d2d_reset_o_0` :146 | out | 1 |
| `role_is_master_o` :203 | `role_is_master_o_0` :147 | out | 1 |
| `role_locked_o` :204 | `role_locked_o_0` :148 | out | 1 |
| `servo_locked_o` :205 | `servo_locked_o_0` :149 | out | 1 |
| `tl_ewma_credit_o` :206 | `tl_ewma_credit_o_0` :150 | out | 13 |

Clock/reset are also identical: `sys_fclk`, `sys_sysresetn`, `sys_poresetn`,
`sys_hclk`, `sys_hresetn`, `sys_scanenable`, `sys_testmode`, `sys_sysresetreq`
(`nanosoc_eth_chiplet.sv:51-58` / `nanosoc_compute_chiplet.sv:73-80`).

`chiplet_d2d_decode.sv` is **byte-identical** in both repos (12202 bytes;
`diff` is empty), as is `tidechart_shim.sv` (10110 bytes).

### The two structural asymmetries

**Compute link 1 is uncabled.** The compute die has a second TideLink
(`nanosoc_compute_chiplet.sv:865`, `u_tidelink_1`) with a full second pad ribbon.
On a two-board bench only link 0 is connected. The testbench holds link 1's RX
pads at 0 and asserts `link_active_o_1` stays low — a link that trained against
an open ribbon would be a real bug.

**No stimulus port on the compute die.** This is F2 and is covered in §4.

---

## 3. F1 — the module-namespace collision (solved in the build)

Both SoCs are rendered by the same generator from different system descriptions,
so they emit **modules with the same name and different content**. Verilog has
one global module namespace. Concatenating both flists makes the effective
netlist a property of declaration order — the exact failure mode the ethernet
repo already refuses elsewhere (`flist/resolve_tidelink_flist.py:14-39`).

Measured over the two flattened SoC flists (400 vs 233 files, 156 shared
basenames): **145 identical, 11 differing**. Adding the two TideLink trees takes
the total to **39 differing**.

The decisive ones:

| Module | Difference | Consequence if the wrong copy wins |
|---|---|---|
| `PHC_AHB` | Ethernet copy has six outputs the compute copy lacks: `seconds_o`, `nanoseconds_o`, `sub_nanoseconds_o`, `ha1588_servo_en_o`, `sync_interval_o`, `pps_out`. Eth: `nanosoc-multicore-system/ptp-hardware-clock-ahb/src/rtl/phc_ahb.sv:22`; compute: `nanosoc-compute-system/compute-subsystem/src/rtl/wrappers/phc/phc_ahb.sv:22`. | **Hard elaboration error.** The compute variant is the documented "PHC LIVE-TIME LIMITATION" (`nanosoc_compute_chiplet.sv:48-56`). |
| `nanosoc_ss_cpu_plus` | Ethernet: `assign system_hreadyout = cpu_0_hready;` Compute: `assign system_hreadyout = 1'b1;` | **Silent and severe.** That net is the `eth_ss_0` passthrough's HREADY. If the compute copy wins, the ethernet die's stimulus port stops honouring wait states and every transaction in the bench becomes untrustworthy. |
| `nanosoc_region_bootrom` | Ethernet instantiates `nanosoc_bootrom_chip_core`; compute instantiates `nanosoc_bootrom_cpu_0`. | One die boots the other's ROM, or the child cell is unbound. |
| `phc`, `phc_clock_core`, `phc_apb_regs` | 107 / 51 / 35 differing lines. | Wrong PTP hardware on one die. |
| `sl_ahb_sram`, `dma250_qctrl_CFG_MIN` | 25 / 12 differing lines. | Wrong memory / DMA config. |
| 24 TideLink modules incl. `WlinkGenericFCSM_6`, `WavD2DGpio*`, `WlinkGenericFCReplayV2_13`, `tidelink_autoneg`, `tidelink_top` | The two repos pin different TideLink commits — see §6. | One die gets the other's link revision. |

Two are timestamp-only and genuinely benign: `nanosoc_cpu_ss_interconnect.sv`
and `nanosoc_cpu_ss_config_pkg.sv` differ solely in a `// Generated:` comment.

### The fix

`sim/het_pair/gen_libmap.py` emits a Verilog **library map + config**: one
library per die, plus `common_lib` for files byte-identical in both trees. Each
die instance is bound to its own library, and a liblist is inherited by the whole
subtree below an instance (IEEE 1364-2001 §13.3), so two rules partition both SoC
hierarchies completely:

```
instance tb_het_pair.u_dieE liblist eth_lib common_lib work cmp_lib;
instance tb_het_pair.u_dieC liblist cmp_lib common_lib work eth_lib;
```

The order is load-bearing: own library first (wins every collision), then
identical shared files, then `work` (cells VCS resolves from `-y` search
directories — the Cortex-M0+ IP arrives that way), then the other die's library
as a last resort for non-colliding modules one flist simply does not name
(`cmsdk_ahb_to_apb_ipc` is the real example).

Final partition: **common_lib 298 files, eth_lib 286, cmp_lib 117.**

Two build facts worth recording, both established empirically:

* `-libmap` **assigns** files to libraries but does not cause them to be read.
  The files must also reach the compiler. `-libmap` alone gives
  `Error-[CFCILFBI] Cannot find cell in liblist` for every cell; `-libmap` plus
  the same files via `-f` binds correctly. Hence `--out-sources`.
* `resolve_tidelink_flist.py` deliberately emits `+incdir+${TIDELINK_HOME}/…`
  **unexpanded**. With two TideLink checkouts a single ambient `TIDELINK_HOME`
  silently gives one die the other's include path, and `+incdir` is global to a
  VCS compilation with no per-library scoping. `gen_libmap.py` resolves each
  side's variables to that side's tree at generation time. (Checked: no header
  is present in both trees with differing content, so the remaining global
  include search is harmless today. It is not guaranteed to stay that way.)

**`make -C sim/het_pair elab-naive`** runs the identical sources and switches
with only the partition removed, so the claim is reproducible rather than
asserted.

---

## 4. F2 — no stimulus port on the compute die

`nanosoc_eth_chiplet` exports `eth_ss_0_*`, an external AHB slave reaching the
SoC matrix (`nanosoc_eth_chiplet.sv:60-70`). That is how the homogeneous pair
brings **both** its dies up firmware-free: an AHB write to `0x2E03_xxxx` becomes
a TideLink APB write through the chiplet decode's tlapb bridge
(`verif/g2_soc_pair/test_g2_soc_pair.py:22-27`).

`nanosoc_compute_chiplet` has **81 ports and not one of them is an AHB or APB
bus.** The only boundary ingress is QSPI flash, UART and the SWJ-DP. And the
debug port is explicitly fenced off from the D2D windows —
`nanosoc-compute-system/sys_desc/nanosoc_compute_soc.yaml:1062-1081` grants
`dap_m` eleven targets and pointedly not `d2d0`/`d2d1`:

> `# NB: the D2D outbound windows (d2d0/d2d1) are deliberately NOT granted to`
> `# dap_m. Letting the local debugger master off-die is the cross-D2D debug`
> `# hazard flagged on u_dap_ss_0 …`

The inbound path cannot bootstrap it either: `d2d0_m`/`d2d1_m` are granted only
`shared_sram_0` and `ipc_mailbox_0` (`:1091-1099`), so a remote die cannot reach
the local TideLink APB — and the link is not up yet regardless.

**Consequences for this bench:**

* The compute die's egress CAM cannot be programmed ⇒ **the compute→ethernet
  direction is untestable**. Only eth→compute is covered.
* `cal_done` and FCSM state must be read hierarchically on that die, not over a
  bus. The testbench does this.
* The link must come up with **zero pokes on the compute side** — see F3.

**Recommended fix (compute chiplet repo):** export a small external AHB slave on
`nanosoc_compute_chiplet`, mirroring `eth_ss_0_*`, reaching the SoC matrix with
the D2D windows granted. It costs 11 pins on a top that already carries 81 and it
is the difference between a chiplet that can be brought up on a bench and one
that can only be brought up by its own firmware. Note this repo's docs are
currently stale in the opposite direction — `src/rtl/README.md` and
`sys_desc/chip_boundary/README.md` still say the top is "TODO — next phase",
contradicted by the 1146-line RTL file and by `docs/STATUS.md`.

---

## 5. F3 — neither die can lock its role without APB writes

`link_active` is an alias of `role_locked_o` (`tidelink_top.sv:2613`), and
`role_locked` gates the Wlink out of reset
(`axi_chiplet_controller.sv:2832 wire wlink_por_reset = ~poresetn | ~role_locked;`).
So until the role locks, the FCSM cannot leave state 0.

`role_strap_i` does **not** lock the role. It only supplies `role_effective`
(`axi_chiplet_controller.sv:622-631`). `role_lock_reg` has exactly two setters
(`:817-823`): the autonegotiation FSM, or an APB write to `ROLE_CFG`. The
autoneg path requires `nego_en = nego_cfg_reg[0]`, and `nego_cfg_reg` PORs to the
**parameter** `NEGO_CFG_RESET`, which is `7'h00` by default
(`tidelink_top.sv:123`).

**Both chiplet tops take that default.** `nanosoc_eth_chiplet.sv:598` and
`nanosoc_compute_chiplet.sv:665,865` instantiate `tidelink_top` without
overriding it. So both dies boot with autonegotiation parked in `ST_BYPASS` and
require APB role-lock — which, by F2, the compute die cannot receive.

The ethernet repo's own boundary spec says exactly this
(`sys_desc/chip_boundary/nanosoc_eth_chiplet.yaml:211-231`):

> "Firmware must role-lock each die before the link is used. … **Bond
> role_strap if you want a die that comes up correctly with no firmware. That is
> the whole trade.**"

**Recommended fix (both chiplet repos):** instantiate `tidelink_top` with
`NEGO_CFG_RESET = 7'h61` (`nego_en | nego_force_lock | mask_hs_auto_en`) — the
value TideLink's own ASIC DFT wrapper already defaults to
(`tidelink/src/rtl/asic/tidelink_dft_wrapper.sv:137`), and the value under which
`tidelink/cocotb/tidelink_top_pair/test_zeropoke_por.py` proves a pair reaches
bilateral `cal = S_DONE` and `fcsm = 4` with **zero** register writes.

**Workaround used in this testbench.** `tb_het_pair.sv` applies the value as a
**parameter at time 0**, which is how the upstream zero-poke test does it:

```systemverilog
defparam u_dieE.u_tidelink.NEGO_CFG_RESET   = 7'h61;
defparam u_dieC.u_tidelink_0.NEGO_CFG_RESET = 7'h61;
```

`Pair._check_autoneg_armed()` only *reads* the register back and asserts it is
`0x61`, so a build without the defparam fails loudly rather than limping to a
confusing timeout. Setting a design parameter from the testbench changes no
logic and weakens no assertion; it is documented at both sites.

This is enough to get **role lock and `cal_done` on both dies**. It is **not**
enough to reach FCSM state 4 — see F6.

Two related facts, both verified against the RTL, which are why one-sided
bring-up is possible at all:

* The three-write LL bootstrap is **not required from cold POR**. `LL_ENABLE`
  (`0x00027F07`) is bit-for-bit the reset value of the fields it writes
  (`Wlink.v:2513,2519,2570,2577,2583,2589`). The triplet is an idempotent
  soft-reset cycle for re-running on a live link.
* `R8_SLOT0 = 0` is likewise already the POR value
  (`axi_chiplet_controller.sv:2015-2043`).

Keep `apb_debug_unlock_i` **asserted** on the slave die if any external-APB Wlink
writes are retained: driving it low silently makes those writes read-only and
stalls both dies at `fcsm=2` (`axi_chiplet_controller.sv:3599`).

---

## 6. Address maps, apertures and inbound target sets

This is where the two designs disagree most, and it is the reason a homogeneous
pair proves so little about a heterogeneous one.

| | Ethernet chiplet | Compute chiplet |
|---|---|---|
| D2D outbound window | `0x2E000000–0x2FFFFFFF` (32 MB, one) | `0x40000000–0x4FFFFFFF` (link 0) and `0x60000000–0x6FFFFFFF` (link 1), 256 MB each |
| Peer aperture (post-decode) | `0x2F......` | `0x41......` (link 0) / `0x61......` (link 1) |
| TideLink config APB | `0x2E03_0000` | `0x4003_0000` / `0x6003_0000` |
| CAM (addr translator) | `0x2E03_4000` | `0x4003_4000` / `0x6003_4000` |
| TideChart APB | `0x2E04_0000` | `0x4004_0000` (link 0 **only**) |
| Inbound: shared SRAM | `0x2D......` | `0x2D......` — **the one agreement** |
| Inbound: IPC mailbox | `0x23......` | **`0x2A......`** |

Sources: ethernet window
`nanosoc-multicore-system/sys_desc/nanosoc_multicore_soc.yaml:291-300`; compute
windows `nanosoc-compute-system/sys_desc/nanosoc_compute_soc.yaml:1017-1018`,
confirmed in generated hardware at
`build/compute_soc/build_soc/rtl/compute_ahb_interconnect/compute_ahb_interconnect/compute_matrix_decode_COMPUTE_M.v:403-409`.
Compute inbound confinement:
`.../compute_matrix_decode_D2D0_M.v:203-212` — two branches (`0x2A`, `0x2D`)
and a default slave. Ethernet inbound: `nanosoc_multicore_soc.yaml:2383-2387`,
`:2167` (mailbox `0x23`), `:2169` (SRAM `0x2D`).

The ethernet SoC's choice of `0x2E` over TideLink's reference `0x40` is
deliberate (`nanosoc_multicore_soc.yaml:291-300`): *"A D2D aperture at
`0x40000000` would be swallowed by `u_ethmac_0` before it ever left the
subsystem."* The compute SoC has no ethernet subsystem and went back to the
reference base. The mailbox difference is equally deliberate — `0x22000000–
0x23FFFFFF` is the Cortex-M4's SRAM bit-band alias, so the compute SoC could not
put its mailbox at `0x23` (`nanosoc_compute_soc.yaml:985-988`).

### Consequences the bench must respect

1. **`0x2D` is the only address both designs agree on.** An eth→compute peer
   write with CAM rule `0x2F→0x2D` lands in the compute die's real
   `shared_sram_0`. This works.
2. **The mailbox needs a different rule.** `0x2F→0x2A`, not `0x2F→0x23`.
3. **The ethernet die has ONE aperture byte.** Its peer window is all of `0x2F`
   (`chiplet_d2d_decode.sv:138 wire a_peer = haddr[24];`), and CAM rules rewrite
   whole bytes (`[15:8]` match, `[23:16]` replace). So it can reach exactly one
   remote 16 MB target at a time; reaching both the far SRAM and the far mailbox
   means reprogramming `RULE_0` between them. The tests do this.
4. **A rule copied from the homogeneous pair is a confinement bug.** `0x2F→0x23`
   is legitimate on an eth↔eth pair. On this pair it must be **DECERRed** by the
   compute SoC's matrix default slave. `test_inbound_confinement_negative`
   asserts exactly that, and it is the single most valuable test in the suite —
   a silent OKAY would mean a cross-die write reaching an unintended target on
   the compute die.

### F4 — the compute peer-aperture defect

`chiplet_d2d_decode` is instantiated with the full untranslated address
(`nanosoc_compute_chiplet.sv:501,528`) and decodes only `haddr[24]` and
`haddr[19:16]` (`chiplet_d2d_decode.sv:112-140`). It is therefore base-agnostic
and re-lands verbatim on the `0x40` window — but **shifted by 16 MB**:

| CPU address (link 0) | `haddr[24]` | Decodes as |
|---|---|---|
| `0x4000_0000` | 0 | **`hsel_tx`** — TideLink TX aperture |
| `0x4003_0000` | 0 | `hsel_tlapb` |
| `0x4100_0000` | 1 | **`hsel_peer`** — the real peer aperture |

The compute firmware stores to `0x4000_0100`
(`nanosoc-compute-system/compute-subsystem/firmware/common/compute_mem.h:204`,
`firmware/app/main.c:43-44`), which lands in TideLink 0's **TX RAM** at
`haddr[13:0]`, not in `ahb_sub`. With the link down it takes a clean ERROR via
the `tx_open` gate (`chiplet_d2d_decode.sv:131,139-140`); **with the link up it
succeeds silently at the wrong destination.**

Every existing compute test passes because both peer-aperture benches bypass the
decoder entirely — `verif/g2_soc_peer_aperture/tb_soc_pair.sv:163-164` wires the
whole 256 MB window straight into `ahb_sub`, and
`verif/g2_peer_aperture/README.md:106-108` states *"`chiplet_d2d_decode` is
deliberately absent"*. **No compute test puts the decoder in the SoC→TideLink
path.** A secondary consequence: only 32 MB of the 256 MB window is uniquely
decoded; it aliases 8× across `0x40..0x4F` because bits `[27:25]` are ignored.

**Recommended fix:** either set `COMPUTE_D2D0_PEER_BASE = 0x41000000` (and the
CAM match byte to `0x41`), or re-parameterise `chiplet_d2d_decode` so the
peer/config split is expressed relative to the window base rather than a
hard-coded `haddr[24]`. The second is better — the decoder is currently shared
verbatim between two designs whose window bases differ, which is how this arose.

---

## 7. TideLink and TideChart revision divergence

The two repos pin **different TideLink commits**, and they have diverged:

| | Ethernet checkout | Compute checkout |
|---|---|---|
| `tidelink` | `884c4a8` (`freeze-2026-07-22-61-g884c4a8`) | `3f3de09` (`v2026.07.16-chiplet-verified`) |
| `tidechart` | `585e042` | `f7bc745` |

`diff -rq` over `tidelink/src/rtl` reports **33 differing files**, including
`WlinkGenericFCSM_6.v`, `WavD2DGpio*.v`, `WlinkGenericFCReplayV2_13.v`,
`tidelink_autoneg.sv` and `WlinkRxLinkLayer.v` — i.e. the FCSM, the GPIO PHY, the
replay CDC and the negotiation logic. The ethernet checkout is roughly a week
newer and carries the recovery/wedge work. The compute repo's
`docs/PIN_POLICY.md` still asserts its tidelink pin is *"the SAME pin the
ethernet-chiplet template uses"* — that is no longer true.

The ethernet repo additionally swaps in a local override of `tidelink_top.sv`
carrying the `ahb_sub` peer-read pipe-offset fix
(`flist/resolve_tidelink_flist.py:130-147`, `patches/0003`). The compute repo has
no `local_overrides` directory and takes the stock file.

**The library partition handles this**: each die gets the TideLink revision its
own repo was verified against, which is a more faithful model of two chiplets
taped out at different times than forcing both onto one revision would be. But it
is a live risk for the real bench — **two dies running different link-layer
revisions is a cross-die protocol-compatibility question nobody has tested**, and
it should be resolved by converging the pins before tapeout, not by relying on
the pair happening to interoperate.

---

## 8. What the testbench does

`sim/het_pair/tb_het_pair.sv` instantiates both tops as shipped, forks nothing:

* eth `pad_clk_tx`/`pad_tx` → compute `pad_clk_rx_0`/`pad_rx_0` and back, through
  TideLink's own `pad_skid` (`SKID_BITS` models ribbon flight time).
* I2C sideband wired-AND between the eth pair and compute link 0.
* eth `role_strap_i = 0` (master, `nego_priority 0x8000`); compute
  `role_strap_i_0 = 1` (slave, `0x7FFF`); distinct `puf_seed`s.
* Compute link 1 RX held at 0, straps benign — asserted to stay down.
* Per-die unprogrammed QSPI flash, so both boot cores halt on the magic mismatch
  and leave both buses free. Firmware-free, as `g2_soc_pair` is.
* Per-die `pad_en` and `sysresetn` so a test can skew one die's reset against the
  other (the far-die-in-reset wedge case).

`sim/het_pair/test_het_pair.py` — seven tests, listed with their claims in
`sim/het_pair/README.md`.

**Known modelling gap:** both dies share `sys_fclk` and `ref_clk`. On a bench
each board has its own oscillator. A skewed-clock variant is the obvious next
step and is where PHY/CDC problems will show up.

---

## 8a. Results actually obtained

Run on this host with **VCS T-2022.06-SP2_Full64**, cocotb 1.7.2, against the
working checkouts (ethernet `01841a4`, compute `891811d`).

| Step | Outcome |
|---|---|
| Flist preparation, both chiplets | **OK** — the compute SoC flist needs `SOCLABS_NANOSOC_SOC_DIR` pointed at `nanosoc-compute-system/build/compute_soc`, not the submodule root, or `dma250_rendered.flist` is unfound |
| Library partition | **OK** — common_lib 298, eth_lib 286, cmp_lib 117; 39 collisions kept private |
| **`make het-pair` (elaboration)** | **PASS** — `simv_het` built, **302 modules, 0 errors**, ~28 s |
| `make elab-naive` (counter-experiment) | **FAILS as predicted**, 10× `Error-[UPIMI-E]` |
| `test_smoke_harness` | **PASS** (84.5 µs sim, 1.0 s wall) |
| Role negotiation, strap-only | **PASS** — opposite roles resolved, zero pokes on the compute die |
| `cal_done`, both dies | **PASS** — with the upstream 500 000-cycle budget |
| LL bootstrap (legacy recipe) | **HANGS the AHB matrix** — finding F5, now skipped by default |
| FCSM=4, with `NEGO_CFG` poked *after* reset | **FAILS** — sticks at state 1 on both dies; the value must be a parameter at time 0 (see §5) |
| `test_het_link_brings_up` full assertions | With the `defparam` fix in place, not yet confirmed green in a completed run |

### The counter-experiment is conclusive

With byte-identical sources and switches and only the library partition removed:

```
Error-[UPIMI-E] Undefined port in module instantiation
  .../nanosoc-multicore-system/build_soc/rtl/nanosoc_multicore_soc.sv, 1036
  Port "seconds_o" is not defined in module 'phc_ahb' defined in
  ".../NanoSoC-Compute-Chiplet/.../compute-subsystem/src/rtl/wrappers/phc_ahb.v", 29
```

The **ethernet** SoC's `u_phc_0` bound the **compute** chiplet's `phc_ahb`.
Four ports fail this way (`seconds_o`, `nanoseconds_o`, `sub_nanoseconds_o`,
`sync_interval_o`). This is F1 caught in the act, and it settles the question:
the two tops cannot share a Verilog module namespace.

### Autonegotiation resolves correctly across the heterogeneous pair

The most encouraging result. With `NEGO_CFG_RESET` armed to `7'h61` and **no
pokes at all on the compute die**, the two dies negotiated to opposite roles from
their straps alone:

```
[ 65.07 us] u_dieC.u_tidelink_0...u_autoneg  state 2 -> 5  (won=0 lost=1 done=1 err=0)
[260.13 us] u_dieE.u_tidelink  ...u_autoneg  state 4 -> 9  (won=1 lost=0 done=1 err=0)
```

The compute die **lost** (slave, `role_strap_i_0 = 1`) and the ethernet die
**won** (master, `role_strap_i = 0`), exactly as the straps specify. Role lock
completes on both. This is direct evidence that F3's recommended one-parameter
change is sufficient to make the heterogeneous pair bring itself up, and that
F2's missing bus does **not** block link bring-up — only CAM programming and the
reverse data direction.

Note the asymmetry in timing: the slave resolves at 65 µs, the master not until
260 µs. That ~195 µs gap is worth understanding before the bench — a host script
that polls for `role_locked` on both boards needs a timeout comfortably beyond
the master's path, and the ordering is the opposite of the intuitive one.

### `cal_done` converges on the heterogeneous pair

With the budget raised to the proven upstream value (500 000 cycles;
`pair_v2_common.py:252`) and the calibrator sim-bypass re-applied after each
SoC's PRMU releases `poresetn`, **`cal_done` asserts on both dies.** Reset →
role lock → `cal_done` all pass. Budgets matter here: the first attempt used
10 000 cycles, 50× too small, and timed out on a link that was converging
normally.

### F5 — the manual LL bootstrap hangs the bus when autonegotiation is armed

**New finding, measured.** After `cal_done`, the ported bring-up issues the
homogeneous pair's three-write LL bootstrap plus `R8_SLOT0 = 0`. On this pair the
**first** of those writes (`R8_SLOT0` @ `0x2E03_2100`) never completes — the
cocotbext-ahb master times out after 50 000 clock cycles at ~3.48 ms sim, while
the ethernet die's autoneg FSM is still advancing (states 11→12→13→14 observed
between 1.67 ms and 2.40 ms).

The bus is demonstrably healthy: *reads* of the same register file succeed
throughout `wait_cal_done`. It is specifically an external **write** landing
while the negotiation FSM owns the register file.

Those writes are also **unnecessary** from a cold POR — `LL_ENABLE`
(`0x00027F07`) is bit-for-bit the reset value of the fields it writes
(`Wlink.v:2513,2519,2570,2577,2583,2589`) and `R8_SLOT0 = 0` is already the POR
value (`axi_chiplet_controller.sv:2015-2043`). So the testbench now skips them
(`bring_up(ll_bootstrap=False)`, the default) and `bring_up(ll_bootstrap=True)`
reproduces the hang.

**This matters for the bench, not just for sim.** It means the manual bring-up
recipe and autonomous negotiation are mutually exclusive: a host script that
arms `NEGO_CFG` for autonomy *and* replays the legacy LL bootstrap will hang the
die's AHB matrix on the first write. Whichever posture the chiplets tape out in,
the host bring-up code must match it — and `docs/BRINGUP_GAPS.md` should carry
this as a hard rule.

### F6 — where it actually stops: FCSM stalls at state 1

> **SUPERSEDED 2026-07-29 — see `docs/F6_ATTRIBUTION.md` and
> `docs/TIDELINK_HANDOVER.md`.** F6 is now attributed: **TideLink-side**, and two
> claims below are measurably wrong.
>
> 1. **It is not a heterogeneous-pair finding.** It reproduces exactly on the
>    *homogeneous* `verif/g2_soc_pair` bench — two identical `nanosoc_eth_chiplet`
>    dies on the same TideLink commit — as soon as the bring-up posture is
>    switched from manual `ROLE_CFG` to autonomous `NEGO_CFG = 0x61`. The
>    discriminating variable is the **posture**, not heterogeneity. Manual
>    posture reaches FCSM 4 in ~90 µs; autonomous does not. Cross-revision
>    interop (`3ed78fe` vs `3f3de09`) is therefore eliminated, and the
>    "force both dies onto one revision" experiment proposed below is unnecessary.
> 2. **The FCSM does not stall at 1.** State 1 is a transient during training.
>    Autonomous training *completes* at 3800 µs (`ST_TRAIN_DONE`, `train_ok = 1`,
>    all 8 peer lanes locked), and its final software reset drops both dies'
>    FCSMs to **0**, where they stay. The measured stuck signals are
>    `io_tx_reset` / `io_rx_reset` / `io_app_reset` on
>    `…u_wlink.tl2wl.wlink_tidelinktl`, held at 1 on **both** dies from 3800.6 µs
>    to 8600.6 µs while `io_app_enable = 1` and `u_autoneg.swreset_hold_r = 0`.
>    The suite's 2.585 ms sample is taken mid-training, 1.2 ms too early.
>
> The prime suspect below — `tidelink_top` hard-coding `apb_debug_unlock_i` /
> `mask_hs_bypass_i` — is **eliminated**: it is identical in the working and the
> failing configurations.

**This is the current blocker and it is unresolved.** With autonomous
negotiation armed, the pair reliably reaches:

- `role_locked` on both dies, with the correct polarity (eth master, compute
  slave), from straps alone;
- `calibration_done` on both dies;

and then the Wlink flow-control state machine
(`u_chiplet_controller.u_wlink.tl2wl.wlink_tidelinktl.state`) **sits at state 1
on both dies** and never reaches state 4. Observed identically in two runs, at
~2.58 ms sim:

```
AssertionError: FCSM did not reach state 4 on both dies (eth=1, compute=1)
```

Two candidate explanations were tested and **both are wrong**, so neither should
be re-tried:

1. *"The LL bootstrap is needed to drive the FCSM."* It cannot be used: with
   negotiation armed, its first write hangs the bus (F5). It is also a no-op
   from cold POR by inspection of the reset values.
2. *"`NEGO_CFG` must be a parameter at time 0, not a post-reset poke."* Making it
   a `defparam` (verified: both controllers read `0x61` from POR) changes
   nothing — the FCSM still stalls at 1. The poke and the parameter give
   identical results up to and including `cal_done`.

**What to investigate next**, in order:

- `tidelink_top` hard-codes `.apb_debug_unlock_i (1'b1)` and
  `.mask_hs_bypass_i (1'b1)` into `axi_chiplet_controller`, **ignoring the
  chiplet boundary ports of the same name**. With the mask handshake bypassed,
  `mask_hs_auto_en` in `NEGO_CFG` may never do its part of the sequencing. This
  is the most likely cause and it is a TideLink integration question, not a
  chiplet one.
- Compare against `tidelink/cocotb/tidelink_top_pair/test_zeropoke_por.py`, which
  *does* reach `fcsm = 4` with zero pokes — but on the raw `tidelink_top` pair,
  not through the chiplet integration. Diffing the two environments' straps and
  reset ordering is the fastest route to the answer.
- The two dies also run **different TideLink revisions** (§7). A negotiation or
  link-enable handshake that changed between `3f3de09` and `884c4a8` would
  present exactly like this. Re-running with both dies forced onto one revision
  would isolate it.

Until F6 is closed, the data-plane tests cannot pass, because the aperture is
only open once the link is up.

### Where it stops

The remaining data-plane tests
(`test_peer_write_eth_to_compute`, `test_peer_sequence_eth_to_compute`,
`test_ipc_mailbox_eth_to_compute`, `test_cam_disabled_is_identity`,
`test_inbound_confinement_negative`) are written and correct-by-construction
against the maps in §6. They need no new infrastructure — the elaboration, the
harness and the bring-up path are all proven.

**Full-suite run, measured 2026-07-29** (clean checkout, VCS T-2022.06-SP2):

| Test | Status | Sim time (ns) | Real (s) |
|---|---|---:|---:|
| `test_smoke_harness` | **PASS** | 84,510 | 1.1 |
| `test_het_link_brings_up` | FAIL | 2,585,440 | 43.0 |
| `test_peer_write_eth_to_compute` | FAIL | 2,585,440 | 43.1 |
| `test_peer_sequence_eth_to_compute` | FAIL | 2,585,440 | 43.9 |
| `test_ipc_mailbox_eth_to_compute` | FAIL | 2,585,440 | 45.6 |
| `test_cam_disabled_is_identity` | FAIL | 2,585,440 | 46.7 |
| `test_inbound_confinement_negative` | FAIL | 2,585,440 | 45.0 |
| **TESTS=7** | **PASS=1 FAIL=6** | 15,597,150 | **268.4** |

**All six failures are F6 and nothing else.** Every data-plane test fails with
`AssertionError: link layer never came up` at an *identical* sim time — they gate
on FCSM=4 before touching the aperture, so none of their own logic has executed
yet. Closing F6 is expected to convert all six in one step; until then these
results say nothing about the address maps in §6.

> **Cost note:** each link-gated test spends ~45 s of wall clock reaching the
> same timeout. Only `test_smoke_harness` (1.1 s) is usable as a fast harness
> check while F6 is open.

---

## 9. Bring-up gap list (for `docs/BRINGUP_GAPS.md`)

Ordered by what blocks the bench soonest.

1. **FCSM stalls at state 1** (F6) — *the current blocker*. Role-lock and
   `cal_done` succeed; the link layer never reaches state 4, so no data-plane
   test can pass. Prime suspect: `tidelink_top` hard-codes `apb_debug_unlock_i`
   and `mask_hs_bypass_i` to `1'b1`, ignoring the chiplet ports. See §8a F6 for
   the two hypotheses already eliminated.
2. **Compute chiplet has no external AHB port** (F2). Without it the compute
   die's CAM cannot be programmed from a host, on silicon or in sim. The
   compute→eth direction is untestable and the two-board bench can only drive
   one direction.
3. **`NEGO_CFG_RESET = 7'h00` on both tops** (F3). As shipped, neither die can
   lock its role without APB writes, and the compute die cannot receive them.
   One-parameter fix, already the ASIC wrapper's default.
4. **Compute peer aperture is at `0x41`, firmware targets `0x40`** (F4). Silent
   wrong-destination write with the link up. No existing compute test can catch
   it because every one of them bypasses the decoder.
5. **Inbound mailbox bytes differ** (`0x23` eth vs `0x2A` compute). Any bring-up
   script or CAM rule copied between the two repos is wrong. Host-side code must
   parameterise the remote target byte per die type.
6. **Never replay the manual LL bootstrap on a die running autonomous
   negotiation** (F5). The first external APB write hangs that die's AHB matrix.
   Pick one posture — autonomous (`NEGO_CFG_RESET = 7'h61`, zero pokes) or manual
   (`7'h00` + the full APB recipe) — and make the host code match it. Also note
   the two are not symmetric in timing: the slave resolves ~4× sooner than the
   master, so `role_locked` polling needs margin sized for the master.
7. **TideLink/TideChart pins have diverged** between the two repos. Converge them
   or explicitly qualify cross-revision interoperation.
8. **Compute chiplet docs are stale** — `src/rtl/README.md`,
   `sys_desc/chip_boundary/README.md` and `docs/G2_PAIR_SIM.md` all still say the
   chiplet top is unwritten. Anyone onboarding will be misled.
9. **Single shared clock in sim.** Add an independent-oscillator variant before
   trusting sim results about the PHY.

---

## 10. Reproducing

```sh
make -C sim het-pair \
     ETH_CHIPLET_HOME=/path/to/nanosoc-ethernet-chiplet \
     COMPUTE_CHIPLET_HOME=/path/to/NanoSoC-Compute-Chiplet
```

Requires VCS (or another simulator with Verilog library-map + config support).
Verilator cannot build this — it rejects duplicate module definitions, which is
the problem being solved. Build artifacts land in `sim/het_pair/build/` and
should be gitignored (`sim/*/build/`, `sim/**/__pycache__/`,
`sim/**/results.xml`, `sim/**/ucli.key`, `sim/**/novas.*`, `sim/**/verdi*`).

## 10a. Bootstrap hazards  **[VERIFIED 2026-07-29 on a clean clone]**

`make deps` is deliberately shallow (see `scripts/bootstrap.sh`) — it does not
fetch what the sim needs. **Run `make deps-full` before any sim target.** Three
traps, all hit and cleared on a genuinely fresh checkout:

| # | Trap | Symptom | Fix |
|---|---|---|---|
| 1 | **4th-level submodules empty** — `deps/<chiplet>/tidelink/deps/{axi-chiplet-controller,tidelink-phy,tidelink-gpio-phy}` | Flists reference RTL inside them (`WavResetSync.v`), so VCS fails **late** with `Error-[CFCILFBI] Cannot find cell in liblist`, naming a cell rather than a missing file | `make deps-full`; `sim/het_pair/Makefile` now preflights for empty dirs and says so up front |
| 2 | **eth-chiplet's TideLink pins a `tidelink-gpio-phy` commit its GitHub remote does not have** (`6ee8418…`) | `fatal: remote error: upload-pack: not our ref 6ee8418…`. The compute side fetches the *same* commit fine — it points at the GitLab mirror instead | Repoint that submodule at `https://git.soton.ac.uk/soclabs/tidelink-gpio-phy.git`, or fetch the object from an existing local checkout. **This is an upstream defect in the eth chiplet: a fresh clone of it cannot bootstrap.** |
| 3 | **Vendor flash model not fetched on the compute side** — `ahb_qspi/verif/VIP/SST26VF064B.v` | `[gen] ERROR: …/SST26VF064B.v missing` during compute SoC generation. The eth side auto-downloads it; the compute side's `make -C ahb_qspi get_flash_model` needs its env sourced first (`makefile:1: /make.cfg: No such file`) | Source the compute `set_env.sh` before that make, or copy the model in |

> **The stale-build trap applies here.** An earlier `simv_het` can survive a
> regenerated flist and make a broken tree look green. Always `rm -rf
> sim/het_pair/build` before trusting an elaboration result.

---

*A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
license. Copyright 2026, SoC Labs (www.soclabs.org).*
