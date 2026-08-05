# FPGA test programme — the heterogeneous eth ↔ compute chiplet pair

**Scope:** what to test on **two KR260 boards joined by a J21 ribbon**, now that
both chiplets have bitstreams. This document owns the *FPGA* programme only. The
planning namespace is [`TEST_MATRIX.md`](TEST_MATRIX.md); every id proposed here
is new and collision-checked against **both** namespaces (see §2.0).

Read with [`SAFETY.md`](SAFETY.md) (hazards — non-negotiable),
[`ETH_COMPUTE_BRINGUP.md`](ETH_COMPUTE_BRINGUP.md) (the pair runbook),
[`BENCH_RUNBOOK.md`](BENCH_RUNBOOK.md) (mechanics),
[`F6_ATTRIBUTION.md`](F6_ATTRIBUTION.md) (why autoneg is not an option), and
[`SIM_PLAN.md` §9a–§9d](SIM_PLAN.md) (what sim already proves).

> **Terminology warning — two different things are called "H2" in this repo.**
> [`SAFETY.md`](SAFETY.md) **H2** = *peer access on a down link hangs the bus*.
> [`ETH_COMPUTE_BRINGUP.md`](ETH_COMPUTE_BRINGUP.md) borrows **H2** from the
> compute SoC's own hazard list to mean *`ps_m` deliberately excludes
> `d2d0`/`d2d1`* (`nanosoc_compute_soc.yaml:1106-1123`). This document writes the
> second one as **`H2-PSM`** throughout and never bare "H2".

---

## 0. Read this before you book bench time

### 0.1 ★ `F7` — the compute die cannot role-lock on FPGA, so the het link cannot come up

**This is the finding that reorders the whole programme.** It was established
read-only from the two chiplet repos while writing this document; it is not in
any existing doc, and it contradicts
[`OVERNIGHT_REPORT.md` §6](OVERNIGHT_REPORT.md)'s ranking.

`role_locked` is a **mutual clock enable** — `wlink_por_reset = ~poresetn |
~role_locked`. Until it latches, the Wlink's tx/rx/app resets are held, the
forwarded `pad_clk_tx` and the calibrator are in reset, and `cal_done` can never
assert. And because `cal_done` on **either** die gates on the peer training over
the ribbon, one die that cannot role-lock takes the **whole pair** down.

`role_lock_reg` latches on exactly three terms
(`.../tidelink/src/rtl/local_overrides/axi_chiplet_controller.sv:880-892`):

```verilog
if ((nego_lock_pending_reg && mask_hs_gate_open) ||   // (a) genuine I²C peer-mask handshake
    (nego_lock_pending_reg && nego_lost_w)      ||   // (b) autoneg ran and lost
    (nego_lock_pending_reg && SELF_ARM_TRAIN_EN))    // (c) the I1 self-arm parameter
    role_lock_reg <= 1'b1;
```

with `mask_hs_gate_open = mask_hs_match | mask_hs_bypass_i` (`:725`), and
`nego_lock_pending_reg` set by a SW W1S of `ROLE_CFG[1]` (`:809-810`).

On the **built compute KR260 bitstream** all four routes are closed:

| # | Term | State on `kr260-compute-chiplet` | Source |
|---|---|---|---|
| 1 | SW W1S of `ROLE_CFG[1]` reaches the register at all | **NO** — `ps_m` omits `d2d0`/`d2d1`, so PS cannot touch `0x4003_2080` | `nanosoc_compute_soc.yaml:1110`; measured `SIM_PLAN.md` §9c C1; `targets.py` `ps_reaches_d2d=False` |
| 2 | (a) `mask_hs_bypass_i` | **tied `1'b0`** | `tidelink/fpga/vivado_ip/nanosoc_compute_chiplet_vivado_wrapper.v:259` |
| 3 | (b) `nego_lost_w` — needs autoneg armed | **`nego_en = 0`**: `NEGO_CFG_RESET = 7'h00` default, not overridden | `axi_chiplet_controller.sv:79,654,760`; `nanosoc_compute_chiplet.sv:698` |
| 4 | (c) `SELF_ARM_TRAIN_EN` | **`1'b0`** (parameter default; not overridden) | `nanosoc_compute_chiplet.sv:698` — `tidelink_top #(.NUM_PHY_LANES(NUM_PHY_LANES)) u_tidelink_0` |

Contrast the eth die, which **does** come up PS-side on silicon:

```verilog
// nanosoc_eth_chiplet.sv:615
tidelink_top #(.NUM_PHY_LANES(NUM_PHY_LANES), .SELF_ARM_TRAIN_EN(1'b1),
               .AUTO_ANCHOR_EN(1'b1), .TXGEN_PRESENT(1'b0)) u_tidelink (
```

The eth FPGA build ties `mask_hs_bypass_i = 1'b0` and `apb_debug_unlock_i = 1'b0`
too (`nanosoc_eth_chiplet_vivado_wrapper.v:283-284`). **`SELF_ARM_TRAIN_EN(1'b1)`
is the only reason a PS-driven `ROLE_CFG` works on a KR260 chiplet build**, and
the compute chiplet does not set it. The parameter's own comment says so:
*"Set 1'b1 ONLY on the eth-chiplet tidelink_top instantiation; every other
integration keeps the 1'b0 default."* (`axi_chiplet_controller.sv:106-107`).

Both facts are pinned to the tree that produced the shipped bitstream:
compute `tidelink` HEAD is `74c6777`, the exact commit cited in the built
`.hwh` provenance in `hetsoc.toml.example`, and
`src/rtl/local_overrides/axi_chiplet_controller.sv` is in
`tidelink/flists/tidelink_fpga.flist:286`, so the override is the RTL that built.

#### Consequences

* **`L3-LINK-01` (het link comes up) is unreachable on FPGA today**, and with it
  every L3/L4/L5 row. This is not a bench-technique problem; no bring-up ordering,
  ribbon reseat or POR sequence fixes it.
* **`C1` is necessary but NOT sufficient.**
  [`OVERNIGHT_REPORT.md` §6](OVERNIGHT_REPORT.md) ranks C1 (two lines of yaml) as
  the cheapest unblock. With C1 alone, a PS write of `ROLE_CFG = 0x03` would set
  `nego_lock_pending_reg` and then **nothing would ever open the gate** — the
  failure would move from "register reads 0" to "register reads back but
  `role_locked` stays 0", which is *harder* to diagnose, not easier.
* **Firmware does not rescue it either.** `manager_m`/`compute_m` reach `d2d0`
  (`SIM_PLAN.md` §9b) so firmware could issue the W1S — but it would hit the same
  dead gate. `G-FW` is not an alternative to the RTL/build change; it is
  downstream of it.
* **`dap_m` is also excluded from `d2d0`/`d2d1`** (`nanosoc_compute_soc.yaml:1064`),
  so an SWD probe cannot write `ROLE_CFG` directly either.

#### The unblock, ranked

| Rank | Change | Where | Size | Why this order |
|---|---|---|---|---|
| **1** | `.SELF_ARM_TRAIN_EN(1'b1)` on `u_tidelink_0` (and `u_tidelink_1` for symmetry) | `NanoSoC-Compute-Chiplet/src/rtl/nanosoc_compute_chiplet.sv:698,899` | 1 line ×2 | The **proven** eth path (I1). Does **not** set `mask_hs_verified_reg`, so the integrity witness stays honest — the RTL calls `mask_hs_bypass_i` "the SHAM opener" (`:711-725`) |
| **2** | Add `d2d0`, `d2d1` to `ps_m`'s target list (**C1**) | `nanosoc-compute-system/sys_desc/nanosoc_compute_soc.yaml:1110` | ~2 lines | Without it nothing can *issue* the W1S from the bench |
| 3 | Rebuild both compute targets + re-cite the `.hwh` in `hetsoc.toml` | compute FPGA flow | Vivado hours | 1 and 2 are useless until the bitstream carries them |
| — | *(alternative to 1)* `mask_hs_bypass_i_0 = 1'b1` | `nanosoc_compute_chiplet_vivado_wrapper.v:259` | 1 line | Cheaper to reach (BD, not SoC RTL) but forges the peer-mask gate. Prefer 1 |

**1 + 2 + 3 together are the single highest-value item in this programme.**
Everything in §2 Tier 2 and beyond is gated on them. Until they land, the het
pair is a **single-board** programme (Tier 0 and Tier 1 only), and the correct
use of two leased boards is the **homogeneous eth↔eth control pair**
(`[pair.eth-homogeneous]` in `hetsoc.toml.example`), which does come up.

> **Do not spend a bench session discovering F7.** `L0-BUILD-01/02/03` (§2.1)
> assert it offline, in CI, in seconds. Write those first.

### 0.2 `F8` — the compute die_a/die_b labels are inverted between two docs, and the strap is wrong for the het pair

[`ETH_COMPUTE_BRINGUP.md` §0](ETH_COMPUTE_BRINGUP.md) states *"compute:
`kr260-compute-chiplet` = **die_b (mirrored)**, `-flip` = die_a (straight)"*.
The build says the opposite:

* `tidelink/fpga/targets/kr260-compute-chiplet/tidelink_design.tcl:2`
  — *"KR260 Block Design TCL (**die_a / straight**)"*, `role_strap_const
  CONST_VAL {0}` (`:159`)
* `kr260-compute-chiplet-flip/tidelink_design.tcl:2` — *"(**die_b / FLIP**)"*,
  `CONST_VAL {1}` (`:159-161`)
* `kr260-compute-chiplet/BUILD_NOTES.md:3,122-129` agrees with the TCL.

The **ball map arbitrates the deployment question, and the runbook wins there**:

| Target | `pad_clk_tx` | `pad_clk_rx` | strap |
|---|---|---|---|
| `kr260-eth-chiplet` (die_a) | AD15 / BCM0 | AC14 / BCM8 | 0 |
| `kr260-eth-chiplet-flip` (die_b) | AC14 / BCM8 | AD15 / BCM0 | 1 |
| **`kr260-compute-chiplet`** | **AC14 / BCM8** | **AD15 / BCM0** | **0** |
| `kr260-compute-chiplet-flip` | AD15 / BCM0 | AC14 / BCM8 | 1 |

(XDC `PACKAGE_PIN` lines: eth `kr260_eth_chiplet_tidelink.xdc:20,31`; compute
`kr260_compute_chiplet_tidelink.xdc:58,70` and the flip's `:54,66`.)

Over a straight-through BCM_n↔BCM_n ribbon, the eth die_a image needs a peer that
transmits on BCM8 and receives on BCM0 — that is **`kr260-compute-chiplet`, the
non-flip**. So `HETSOC_COMPUTE_TARGET_B=kr260-compute-chiplet` is
**electrically correct** and must not be changed. But:

* that image carries **role strap 0**, the same as eth die_a, because the compute
  targets were laid out for a *compute↔compute* pair whose die_a/die_b convention
  does not compose with eth's;
* so on the het pair **both dies POR with `role_effective = 0` (master)**
  (`role_effective = role_locked ? role_cfg_reg : … role_strap_i`,
  `axi_chiplet_controller.sv:656-658`);
* so `bench-status`'s documented expectation *"compute die_b → slave(1)"*
  ([`ETH_COMPUTE_BRINGUP.md` §3 step 3](ETH_COMPUTE_BRINGUP.md)) and matrix rows
  `L1-LINK-02` / `L3-LINK-05` are **unmeetable on this pair** — twice over: the
  strap is 0, and `ROLE_STATUS` @ `0x4003_2084` is unreachable from PS anyway
  (`H2-PSM`).

This does **not** by itself break the link — on the eth pair the software
`ROLE_CFG` lock is authoritative and the strap is only the pre-lock default. But
once F7 is fixed, whoever writes the compute-side role lock must write
`ROLE_CFG = 0x03` (slave-lock) **against a strap that says master**, and every
role-related expectation in the harness has to be sourced from the *role the
board was deployed as*, never from the strap. `L0-BUILD-04` and `L2-STRAP-01`
(§2) pin this.

**Naming recommendation** (not for this document to land unilaterally): rename
the compute targets `kr260-compute-chiplet-{straight,mirror}` so the label
carries the ball map rather than a pair-relative role.

### 0.3 What is genuinely ready

Good news, so the above is not read as "nothing works":

* Both bitstreams exist and build clean (compute: `write_bitstream` Complete,
  0 critical warnings, WNS +12.30 ns, 2026-07-31).
* The compute PS backdoor **works** for everything except the D2D window: a
  write + read-back to `shared_sram_0` @ `0x2D002000` returned `0xa5a5beef`
  (`SIM_PLAN.md` §9c). **That is the entire receive-side verdict path** for
  eth→compute, and it is proven.
* Both KR260s were free at the time of writing (`fpgahub status`, 2026-08-05).
* The host framework already encodes `ps_reaches_d2d=False`, the per-direction
  CAM, and the wedge guards.

So Tier 0 and Tier 1 of §2 are runnable **now**, and a large fraction of the
eth→compute data plane is one compute rebuild away.

---

## 1. What FPGA testing adds that simulation cannot

`sim/het_pair` proves nine matrix rows on real RTL from both repos
(`PROVEN-SIM-HET`). It is strong evidence about **logic**. It is *no* evidence
about the following, and every item below is a reason to spend bench time.

| # | What only silicon shows | Why sim structurally cannot | Which sim result it undermines |
|---|---|---|---|
| **1** | **Real clock domains and jitter.** Two independently-generated `user_ref_clk`s, a PLL/MMCM per board, a /8 PHY divider, and an FR4 + ribbon skew budget. The compute die has **two** `user_ref_clk` and **two** `pad_clk_rx` async domains, not one. | The het TB shares idealised generators; the ASIC clock plan for compute is explicitly **unanalysed** (`PHYSICAL_HANDOFF.md:25-29`, via `ARCHITECTURE.md` §7) | all of `L0-SIM-02` |
| **2** | **The runtime forwarded-clock winscan.** The one thing the link's correctness actually rests on: an IDELAY sweep that finds a sampling point against *this* peer's silicon. `test_g2_soc_pair.py:137-142` forces `tb_early_exit_force_q` on both calibrators — **a green sim says nothing at all about calibration**. And a symmetric bypass *deadlocks* the pair in sim (`SIM_PLAN.md` §9a), i.e. the sim's calibration model is known-unfaithful in both directions. | The calibrator is bypassed by construction | `L0-SIM-02` — the timing half is vacuous |
| **3** | **Het eye margin.** Two *different* designs, different placement, different routing, different IO banks driving the same 18 conductors. The eth chiplet already carries residual TideLink RX setup violations (−2.9/−3.3 ns, 4 endpoints, `RISK-8`). There is no reason the margin is symmetric, and no reason it matches the homogeneous pair in either direction. | Sim has no eye | every data-plane row |
| **4** | **Ribbon signal integrity.** An unshielded 40-way IDC loom carrying 8 data lanes + a forwarded clock, with the power rails stripped (`H7`). Crosstalk, reflections, lane-to-lane skew, and — the failure that looks like an RTL bug — a marginal or intermittent conductor. | no physical layer | — |
| **5** | **The intermittent wedge (`H3`/`G-WEDGE`) is a SILICON behaviour.** Root-caused to recovery-stripped AXI FC nodes *plus* one-shot calibration *plus* a marginal eye. Observed at ~2 of 3 repeats. The **first** transfer reliably passes; each subsequent one is progressively more likely to sample one bit wrong. An idealised sim never samples one bit wrong, so it will pass forever. | sim has no BER | `L0-SIM-03/05/10` pass but say nothing about repeatability |
| **6** | **Thermal and long-run drift.** `calibrated_once_q` latches on first `S_DONE` and permanently gates re-trigger, so **the sampling point is frozen at bring-up**. Everything that happens to the eye over the next hour — die self-heating, ambient, supply droop, the ribbon warming — is uncompensated. This is a *time* axis and sim has no time budget beyond milliseconds. | 500,000 cycles ≈ 10 ms was the longest watch ever run (`F6_ATTRIBUTION.md` §2) | `L5-CHAR-01` has no sim analogue at all |
| **7** | **Real reset and power sequencing across two boards.** Two barrel-jack PSUs, two ZynqMP PORs, two PL loads, two independent `poresetn` edges — in an arbitrary order, seconds apart, with a human in the loop. `pad_clk_rx` **is the far die's clock**: it only toggles when the peer is powered and transmitting. | `L0-SIM-17` covers the order but `RISK-6` records that *"an idealised sim resolves demets cleanly and may pass vacuously"* — the false-FULL wedge is documented as **invisible to simulation** (`RESET_ORDERING.md:100-117`) | `L0-SIM-17` explicitly |
| **8** | **Sustained throughput and latency.** Real numbers with variance, over a real PS→AXI→AHB→CAM→XHB500→Wlink→PHY→ribbon path, bounded by `/dev/mem` + SSH round trips at the top. There is no sim number worth quoting. | wall-clock vs sim-time | `L5-PERF-*` |
| **9** | **PS/AXI interaction, and the wedge's blast radius.** The failure that costs bench sessions is not in the chiplet at all: an unreturned `B`/`R` beat saturates the ZynqMP `M_AXI_GP0` SmartConnect and takes down **every PL slave**, with no `SIGBUS` and no kernel message, because `SUB_STALL_TIMEOUT` only counts while `hreadyout` is low. There is no PS in the sim. | no ZynqMP model | `H3` entirely |
| **10** | **The deploy/POR/lease infrastructure as a variable.** `fpgahub` per-board routes 404 from some hosts; group `board reset` trips on the `_pl` topology member; back-to-back PORs give transient "cable not found" (`RISK-9`). A 3-iteration repeatability run has already produced *1 wedge, 2 deploy failures* (`L5-CHAR-02`). | not a DUT property | — |
| **11** | **Build-configuration drift between the two dies.** The eth die builds `SELF_ARM_TRAIN_EN(1'b1)` **and** `AUTO_ANCHOR_EN(1'b1)`; the compute die builds neither (§0.1). So even after F7 is fixed the pair is **asymmetric in its recovery logic**, on the exact nodes that wedge. Nothing in sim distinguishes them because the het TB steps over bring-up entirely. | the manual posture deposits role bits and never exercises either parameter | the `PROVEN-SIM-HET` caveat |

**The one-line headline.** *Simulation proved the het pair's **address maps and
logic**; FPGA is the only place that can test its **timing, its eye, its resets,
and its behaviour over hours** — and every known way this pair fails is in that
second list.*

---

## 2. The prioritised test programme

### 2.0 Conventions, and the id-collision check

Ids follow [`TEST_MATRIX.md`](TEST_MATRIX.md)'s scheme `L<N>-<AREA>-<NN>` with
`AREA` ↔ `tests/test_l<N>_<area>.py` per [`REPO_LAYOUT.md`](REPO_LAYOUT.md).

**Every area below is NEW.** Verified zero occurrences of `L<n>-{BUILD,PHYS,
STRAP,CLK,SEQ,HET,THERM,WEDGE,OBS}-<nn>` across `docs/`, `tests/`, `sim/` and
`host/` (2026-08-05). New areas were chosen deliberately over extending
`L3-LINK-13…` etc., because the two namespaces already have **23 divergent
shared ids** ([`TEST_ID_MAP.md`](TEST_ID_MAP.md)) and adding to a colliding area
makes that worse. Each new area maps 1:1 to a new pytest file, so the planning
and implementation ids can be born identical:

| Area | pytest file | Level |
|---|---|---|
| `L0-BUILD` | `tests/test_l0_build.py` | offline |
| `L1-PHYS` | `tests/test_l1_phys.py` | 1 board, read-only |
| `L2-STRAP` | `tests/test_l2_strap.py` | 1 board, config writes |
| `L3-CLK` | `tests/test_l3_clk.py` | 2 boards, control plane |
| `L3-SEQ` | `tests/test_l3_seq.py` | 2 boards, control plane |
| `L4-HET` | `tests/test_l4_het.py` | 2 boards, **data plane** |
| `L5-THERM` | `tests/test_l5_therm.py` | 2 boards, soak |
| `L5-WEDGE` | `tests/test_l5_wedge.py` | 2 boards, soak |

**Prioritisation principle.** Two leased boards are the scarce resource, so tests
are ranked by **information gained per bench-hour**, with a hard rule: *anything
that can be answered offline must not consume bench time*. That puts a whole
tier of build-artefact assertions **in front of** the first lease, and it is why
Tier 0 exists.

`[TBD]` marks a value this document refuses to invent, always naming the file
that must supply it.

---

### 2.1 Tier 0 — offline, no board, CI-safe · `L0-BUILD` · **do this first**

Zero bench time, seconds of CI, and it is the tier that would have saved the
first het session. All rows are `wedge: none`, `CI: yes`.

| ID | Name | Proves | Method | Prereq | Pass criteria | Effort |
|---|---|---|---|---|---|---|
| `L0-BUILD-01` | **compute can role-lock** | ★ the F7 gate. Fails until the compute rebuild lands, and says exactly why | parse `NanoSoC-Compute-Chiplet/src/rtl/nanosoc_compute_chiplet.sv` for the `tidelink_top #(…) u_tidelink_0` parameter list; parse `nanosoc_compute_chiplet_vivado_wrapper.v` for `mask_hs_bypass_i_0`; parse `nanosoc_compute_soc.yaml` `ps_m` target list | both repos checked out at the pins that built the bitstream | **at least one** of: `SELF_ARM_TRAIN_EN(1'b1)` present, or `mask_hs_bypass_i_0` = `1'b1`, **and** `ps_m` lists `d2d0`. Failure message quotes §0.1 and the three ranked fixes | **S** (½ day) |
| `L0-BUILD-02` | recovery parity across the pair | the two dies ship different FC recovery logic | compare `AUTO_ANCHOR_EN` on both chiplet tops | both repos | records the delta; **non-gating** (a `nongating` marker) — it is a fact to carry into every wedge report, not a regression | S (2 h) |
| `L0-BUILD-03` | window base cited, never assumed | the `G-WIN` discipline holds per bench | assert the resolved `kr260-compute-chiplet` descriptor's `source` names *this bench's own* `.hwh`, not the example string | `hetsoc.toml` present | `source` contains a path that exists on this host and a `MEMRANGE` citation; the shipped example string is **rejected** | S (2 h) |
| `L0-BUILD-04` | strap constant matches the deployed role | catches F8 and a swapped image before power | parse `CONFIG.CONST_VAL` from each deployed target's `tidelink_design.tcl`; compare with the board's configured `role` | `hetsoc.toml` + both target dirs | die_a target → `0`, die_b target → `1`. **On the het pair this FAILS by design** (compute non-flip is strap 0) and must be `xfail` with §0.2 cited, not silently tolerated | S (½ day) |
| `L0-BUILD-05` | ball maps are complementary | the H6 electrical hazard, caught statically | extract `PACKAGE_PIN` for `pad_clk_tx*` / `pad_clk_rx*` (and the 8 data lanes) from both deployed targets' XDCs | both target dirs | die A's TX ball set == die B's RX ball set and vice-versa, for **all 9 conductors**. A partial match fails | **M** (1–2 d — the data lanes need parsing, not just the clock) |
| `L0-BUILD-06` | PHY version match | a V1/V2 het pair is untested (matrix `L3-LINK-10`, done statically) | grep both build logs / flist resolution for `TIDELINK_PHY_V2` | build artefacts retained | both V2, or both V1, and **recorded**. Compute's `BUILD_NOTES.md:40` says `TIDELINK_PHY_V2=1` is **MANDATORY** ("silent-V1 trap"); the eth chiplet was measured building **V1** (`F6_ATTRIBUTION.md` §5.1). **Expect this to fail today** | S (½ day) |
| `L0-BUILD-07` | lane count match | `NUM_PHY_LANES` agreement (matrix `L3-LINK-11`, statically) | compare the parameter on both tops | both repos | both `8` | S (1 h) |

> `L0-BUILD-06` is worth calling out: if the eth die builds V1 and the compute
> die builds V2, the pair is a **cross-PHY-generation link** that nobody has
> simulated. That is a red flag to resolve *before* a bench session, and it is
> free to check.

**Tier 0 total: ~4 engineer-days, zero bench time, and it gates everything.**

---

### 2.2 Tier 1 — one board at a time · `L1-PHYS`, `L2-STRAP` · **runnable today**

Everything here works on the **compute board alone**, with no ribbon and no eth
board, so it can be done on a single lease and it de-risks the pair session. All
`wedge: none` unless noted; all use only in-window accesses.

| ID | Name | Proves | Method (addresses) | Prereq | Pass criteria | Wedge | CI | Effort |
|---|---|---|---|---|---|---|---|---|
| `L1-PHYS-01` | **compute backdoor delivers** | ★ the one compute-side capability everything downstream needs | write then read `shared_sram_0` at compute SoC `0x2D00_2000` (PS `window_base + 0x2D002000`) with a walking-1 pattern over 16 words | compute bitstream, resolved descriptor | every word reads back. This is the **receive-side verdict path** for the whole eth→compute programme, proven independently of the link | none | yes | S (½ day) |
| `L1-PHYS-02` | compute boot-ROM signature | fills matrix `L1-PROBE-02`'s `[TBD]` | read compute SoC `0x0000_0000 + 0x00..0x0C` | `L1-PHYS-01` | **records** the four vector words on first run and pins them thereafter. Expected values `[TBD — must come from the compute build's boot-ROM image / `nanosoc-compute-system` boot sources]`; until then judge on plausibility (`Board.alive()` already does this and warns) | none | yes | S (½ day) |
| `L1-PHYS-03` | the D2D guard fires on real hardware | `L0-ADDR-19`'s guard, on a board | attempt `board.read(0x40032108)` on the compute board | compute board | `AddressGuardError` raised **before** any `/dev/mem` access; board still alive afterwards. The negative that keeps the silent-zeros trap unreachable | none | yes | S (2 h) |
| `L1-PHYS-04` | link-1 window terminates | `G8` — a stray `d2d1` access must not hang the matrix | with the guard temporarily scoped to permit it *(deliberate, single-address, attended once then pinned)*, read compute SoC `0x6000_0000` | compute board, POR staged | in-window undecoded ⇒ `SLVERR`, not a hang; board alive. If it hangs, `G8`'s tie-off is wrong and the **whole compute map** needs re-auditing | **low** | no (attended once, then a recorded fact) | M (1 d) |
| `L1-PHYS-05` | LEDs report link state | a human-visible, zero-cost link indicator | read `led0`/`led1` mapping from `BUILD_NOTES.md:26` (`link_active_o_0`→led0, `role_is_master_o_0`→led1); observe on the board | compute board | LEDs track the state; **documented**, because on the compute die `link_active` is otherwise unobservable (`H2-PSM`) — this is the only compute-side link indication that exists today | none | manual | S (2 h) |
| `L2-STRAP-01` | eth role lock + self-arm, isolated | separates "role lock works" from "the peer is missing" | on the **eth** board alone: write `ROLE_CFG` `0x4_2E03_2080` = `0x02`; read `ROLE_STATUS` `0x4_2E03_2084`; read `OBS_MASK_HS` `0x4_2E03_2194` | eth board, fresh die | `ROLE_STATUS` → `0x02` (`role_locked=1`, `effective_role=0`); `OBS_MASK_HS[18] nego_lock_pending`, `[19] mask_hs_match`, `[20] mask_hs_gate_open` recorded. **On the eth die expect `role_locked=1` with `mask_hs_match=0`** — that is `SELF_ARM_TRAIN_EN` working, and seeing it is the direct positive control for F7 | low | yes | **S** (½ day) — highest value/effort ratio in Tier 1 |
| `L2-STRAP-02` | FCSM responds with no peer | the link SM is live and the failure is *peer absence*, not *this die* | after `L2-STRAP-01`, read `SWI_LANE_STATUS` `0x4_2E03_2108` | `L2-STRAP-01` | FCSM advances `0→1`, `cal_done` stays `0`. Matrix `L2-ROLE-02` is `PROVEN-HOM`; this is the het-session **pre-flight** form of it | low | yes | S (2 h) |
| `L2-STRAP-03` | **`SWI_FORCE_RECAL` exists and is observable** | closes matrix `L3-CAL-02`'s `[TBD offset]` | write `SWI_TRAINING_MODE` `0x4_2E03_2100` with **bit[6]** set (W1P, region 8 slot 0, `axi_chiplet_controller.sv:1219-1222`); poll `OBS_CAL` `0x4_2E03_2198` `[3:0] cal_state` | eth board, link **down** | `cal_state` leaves `S_DONE`. Note the RTL: bit[6] is **write-only, reads back 0**, and the stretch timer is **open-loop** — `OBS_CAL[3:0]` is the *only* authoritative evidence a retrain happened (`:1207`) | low with link down; **HIGH** on a live link | yes (link down only) | S (½ day) |
| `L2-STRAP-04` | TX-aperture gate on real silicon | matrix `L2-LINK-01`, and the negative that makes `L4` survivable | with the link **down**, write eth SoC `0x2E00_0004` | eth board, link down | clean 2-cycle AHB `ERROR` (or a framework refusal); **board alive afterwards**. `L0-SIM-13` proves it in sim on both window bases; this is the silicon instance | low | yes | S (½ day) |

**Tier 1 total: ~5 engineer-days, ~1 single-board bench day.** It converts three
of the biggest unknowns (does the compute backdoor work on *this* board; does the
eth die really self-arm; is there a re-cal primitive) into recorded facts, with
no ribbon and no pair.

---

### 2.3 Tier 2 — two boards, control plane · `L3-CLK`, `L3-SEQ` · **gated on F7**

Everything here needs the link at FCSM=4, so **nothing in this tier is runnable
until §0.1's rebuild lands.** All `wedge: low` — they are RO reads and
config-plane writes; none pushes data across the link.

| ID | Name | Proves | Method | Prereq | Pass criteria | Wedge | CI | Effort |
|---|---|---|---|---|---|---|---|---|
| `L3-CLK-01` | **het calibration converges** | ★ the thing sim cannot test at all (§1 item 2). The winscan trains against a *different design* for the first time | during `bringup()`, sample `SWI_LANE_STATUS` `0x2108` `[16] cal_done` and `OBS_CAL` `0x2198` `[3:0] cal_state`, `[19:4] cal_resweep_ctr` at 20 ms intervals on **both** dies | F7 fixed, ribbon, fresh dies | `cal_done=1` both dies. **Record time-to-cal and `cal_resweep_ctr`** — the eth↔eth reference is `cal_done` at 89.8 µs in sim and first-time-lucky on silicon 2026-07-27; a het pair needing many resweeps is the early warning for a marginal eye | low | yes | **M** (2 d) |
| `L3-CLK-02` | calibration is genuinely one-shot | quantifies `RISK-2` on het silicon | after `L3-CLK-01`, re-read `cal_done`, `OBS_CAL`, `SYNC_DET` `0x2114 [31:16]` and per-node CRC every 60 s for ≥30 min, **no traffic** | `L3-CLK-01` | `cal_done` never re-asserts; `cal_resweep_ctr` frozen. `SYNC_DET` / CRC drift **recorded** — this is the idle-drift baseline that `L5-THERM-01` is differenced against | low | yes (read-only) | M (1 d + 30 min bench) |
| `L3-CLK-03` | forced re-cal on a **down** link recovers it | a recovery primitive that is not a POR | tear the link down (`L5-RECOV-04`-style), then `SWI_TRAINING_MODE` bit[6] on both dies, re-run bring-up | `L2-STRAP-03`, link already down | link returns to FCSM=4 without a JTAG POR | low (link already down) | no | M (1–2 d) |
| `L3-CLK-04` | forced re-cal on a **live** link | whether the eye can be re-centred mid-session — the only software answer to thermal drift | `SWI_TRAINING_MODE` bit[6] on both dies **simultaneously**, link live, no traffic in flight; watch `cal_state` and FCSM | `L3-CLK-03` | link returns to FCSM=4 with a *new* sampling point. ⚠ **Expect this to break the link**: the RTL records that a recal re-entering training mid-FCSM-credit-init *"wedged the master at state 2 with zero TX credit"* (`axi_chiplet_controller.sv:1186-1188`). Run it **last in a session**, with POR staged | **HIGH** | no | M (1 d) |
| `L3-CLK-05` | ribbon integrity pre-check | separates "wiring" from "RTL" before anyone debugs the wrong thing (matrix `L3-LINK-12`, made concrete) | link down, peer driving training: read `WLINK_LINK_STATUS` `0x4_2E03_0234` `[4] rx_valid`, `[3] tx_active`, plus `SWI_LANE_STATUS[7:0] lane_locked` and `[22:21] llrx_state` | both boards, ribbon | `rx_valid` toggles and `lane_locked` is non-zero *while training runs*. A dead `rx_valid` with a live `tx_active` on the peer ⇒ **ribbon/pinout**, not SoC. `llrx_state == 2` ⇒ byte-align error (per `REGISTER_MAP.md:253`) | none | yes | **S** (½ day) — best diagnostic-per-effort in Tier 2 |
| `L3-CLK-06` | per-lane health at link-up | a baseline every later failure is differenced against | at FCSM=4 record `SWI_LANE_STATUS` full word, `[15:8] lane_fault`, `SYNC_DET`, all 21 FC-node registers, `OBS_FC_CREDIT` `0x219C`, `OBS_MASK_HS` `0x2194`, both dies | `L3-CLK-01` | a JSON baseline is written to `build/results/`. `lane_fault == 0x00`. **Non-negotiable prerequisite** for every L5 row | none | yes | S (½ day) |
| `L3-SEQ-01` | **cold-power-order independence** | `RISK-6`, on hardware — the case `L0-SIM-17` is documented as possibly proving *vacuously* | POR board A, wait for it to settle, POR board B, bring up; then swap the order; ×5 each way | F7 fixed, fpgahub per-target POR | link converges either way, ≥5/5. Any false-FULL after ~6 words is `RISK-6` materialising and **stops the session** | low | no (POR-heavy, slow) | M (2 d + ½ bench day) |
| `L3-SEQ-02` | **role-lock on a dead RX clock is refused** | the board-killer guard, tested rather than assumed (matrix `L3-LINK-09`) | with the peer board powered **down**, attempt `pair.bringup()` | one board only | the framework **refuses**. If it does not, add the gate. Then: with the peer down, drive `ROLE_CFG` manually **once**, attended, and check `OBS_MASK_HS[20]` and FCSM — this measures whether the false-FULL class is reachable on *this* build | low → **HIGH** if unguarded | no | M (1–2 d) |
| `L3-SEQ-03` | far-die-dark leaves the near die usable | `L0-SIM-17`'s silicon instance | with the link up, POR board B only; on board A read the whole config plane, `L1-HEALTH-*`, and attempt a TX-aperture write | `L3-CLK-01` | board A stays fully readable; no false `link_active`; the TX gate still errors cleanly. ⚠ this is `H5` (PL-reload one side of a live link) done **deliberately** — never as a side effect | low | no | M (1 d) |
| `L3-SEQ-04` | **CAM does not survive a warm reset** | matrix `L2-CAM-04`'s `[TBD]`, resolved or formally closed | program the CAM; assert `hresetn` alone; read `CAM_CTRL` `0x4_2E03_4004` and `CAM_RULE_0` `0x4_2E03_4010` | a way to pulse `hresetn` alone on KR260 `[TBD — must come from the eth chiplet's PS reset wiring in tidelink_design.tcl / proc_sys_reset usage]` | CAM reads back cleared while `ROLE_STATUS` survives. **If no `hresetn`-only path exists on KR260, close the row as NOT-TESTABLE-ON-FPGA rather than leaving it PLANNED forever** | low | yes if reachable | M (1 d, mostly investigation) |
| `L3-SEQ-05` | bring-up repeatability from cold | matrix `L3-LINK-07` on the het pair | POR both → deploy both → bring up, ×10, JSON per iteration | `L3-CLK-01` | pass rate per stage recorded. The eth↔eth reference is *1 wedge, 2 deploy failures in 3 iterations* (`L5-CHAR-02`) — so **expect a rate, not a pass** | low (no data plane) | no | M (1 d + 1 bench day; ~15 min/iteration) |

**Tier 2 total: ~12 engineer-days, ~2.5 bench days.**

---

### 2.4 Tier 3 — two boards, cross-die data plane · `L4-HET` · **attended only**

> 🔴 **Every row here can wedge both boards** (`H3`). Attended, behind
> `--data-plane` **and** `I_ACCEPT_WEDGE_RISK=1`, POR terminal open on
> `mapstone-dev` before the first peer access. Never in CI, never overnight.
> If the pair wedges **twice on the same test, stop** — that is `G-WEDGE`, an
> RTL fix, not more POR cycles (`SAFETY.md` §5 step 3).

The existing `L4-SRAM-01`, `L4-MBOX-01`, `L4-CONF-01/04` and `L4-DATA-02/07/08`
already specify the eth→compute transfers themselves; **do not renumber them.**
The rows below are what FPGA adds *on top of* those, and what the
one-directional constraint forces.

| ID | Name | Proves | Method | Prereq | Pass criteria | Wedge | Effort |
|---|---|---|---|---|---|---|---|
| `L4-HET-01` | **first-transfer-only discipline** | turns `H3` from an accident into a measurement | after bring-up, do **exactly one** eth→compute write (`CAM 0x002D2F01`; write `0x4_2F00_1000` = `0xC0FFEE01`; compute local read of `0x2D00_1000`), then take a full `L3-CLK-06` health sample and **stop** | `L3-CLK-01`, POR staged | payload lands; FC health unchanged. This is the **minimum viable "the het pair moved data"** and it must be its own test so it can be run in isolation, before anything riskier | HIGH | S (½ day) |
| `L4-HET-02` | wedge-rate at N transfers | the number that decides whether L4 is usable at all | repeat `L4-HET-01`'s single write with `pair.soak(stop_on_degrade=True)` at N = 1, 2, 5, 10, 25, 50, POR-ing and re-bringing-up between each series | `L4-HET-01` | a **wedge-rate-vs-N curve**, not a pass/fail. The eth↔eth reference is ~2 of 3 repeats. This directly measures whether the het eye is better or worse than homogeneous — an open question in `RISK-2` | HIGH | M (1–2 d + 1 full bench day) |
| `L4-HET-03` | health poll predicts the wedge | whether `L5-RECOV-03`'s no-rebuild mitigation actually works | run `L4-HET-02` with `sample_between_transfers()` on; log the FC delta before every transfer | `L1-HEALTH-04`, `L4-HET-02` | **a measurable drop in wedge rate** vs the unguarded loop, and — the real question — whether a rising CRC on `B`/`R` ever *precedes* a wedge or whether the first symptom is the hang itself. A negative result is as valuable as a positive one and must be published either way | HIGH | M (1 d, shares bench time with `L4-HET-02`) |
| `L4-HET-04` | aperture switch without teardown | matrix `L4-MBOX-06`, and the het pair's specific pain: **the eth die has only ONE peer aperture** | SRAM message → quiesce → reprogram `RULE_0.replace` `0x2D`→`0x2A` → mailbox message, no link teardown | `L4-HET-01` | both land. ⚠ `RISK-5`: quiesce and settle **before** the CAM write. On the eth die `peer_aperture_mbox = NO_PEER_APERTURE`, so SRAM and mailbox are **mutually exclusive** and every mailbox demo pays this switch — unlike the compute die, which has a second aperture at `0x44` it cannot use here | HIGH | M (1 d) |
| `L4-HET-05` | the `0x2A` mailbox + IRQ source, on silicon | the het-specific case; `PROVEN-HOM` only at `0x23`, `PROVEN-SIM-HET` at `0x2A` | `map_peer_to(eth, "ipc_mailbox")` → `0x002A2F01`; 4 words at peer `+0x00..0x0C`; `+0x020` = `MSG_VALID`; compute **local** reads of `0x2A00_0000..+0x0C`, `+0x020`, `+0x028` | `L4-HET-04` | words match, `SLOT0_CTRL[0]=1`, `IRQ_STATUS[0]=1`. The verdict is entirely a compute-local read — **no link traversal on the verdict path** | HIGH | S (½ day, reuses `mailbox_send`) |
| `L4-HET-06` | inbound confinement, on silicon | ★ *"never tested anywhere, in sim or on silicon"* was true until `L0-SIM-08`; on **silicon** it is still true | `program_cam(..., allow_unmapped=True)` with replace = `0x2C`, `0x21`, `0x23` (compute has nothing at `0x23`), `0xA0` (CoreSight), `0x00` (code space); one write each, POR between | `L4-HET-01`, POR staged | far die DECERRs; **board not wedged**; the excluded region provably unchanged by a compute local read. The `0x23` case is `L4-CONF-04` and catches a descriptor regression | HIGH (this is the row most likely to wedge — a DECERR *is* an unreturned-response class) | M (1–2 d, and budget **one POR per sub-case**) |

**Tier 3 total: ~6 engineer-days, ~2 attended bench days.** Note that
`L4-HET-02`/`03` will consume most of that bench time in POR cycles, and that
each POR clears the PL and needs **both** boards redeployed (`H5`).

---

### 2.5 Tier 4 — soak and characterisation · `L5-THERM`, `L5-WEDGE`

The tier that only exists because of physics. All attended, all HIGH, none in CI.

| ID | Name | Proves | Method | Prereq | Pass criteria | Effort |
|---|---|---|---|---|---|---|
| `L5-THERM-01` | **thermal drift of a frozen sampling point** | ★ the flagship FPGA-only result. `calibrated_once_q` freezes the eye at bring-up; this measures what an hour does to it | bring up cold; sample every 60 s for ≥2 h with **no cross-die traffic**: `SYNC_DET [31:16]`, `OBS_CAL [19:4] cal_resweep_ctr`, all 7 nodes' CRC (`+0x20`), `lane_fault`, plus PS-side die temperature `[TBD — the ZynqMP SYSMON path; must come from the KR260 platform, e.g. the hwmon sysfs node on the board]` | `L3-CLK-02` baseline | a **CRC/sync-detect-vs-temperature curve**. Traffic-free deliberately, so it isolates *drift* from *load*. Safe to run long because it is read-only — **the only L5 row that is** | M (2 d + 2 h bench) |
| `L5-THERM-02` | drift under load | the same axis with the data plane running | `L5-SOAK-01`-style write-only soak, `stop_on_degrade=True`, 1 h, sampling as above | `L4-HET-02`, `L5-THERM-01` | time-to-first-CRC and time-to-first-wedge, correlated with the `L5-THERM-01` curve. **Attended for the full hour** — this is the row people will be tempted to leave running; do not | M (1 d + 1 bench day) |
| `L5-THERM-03` | cold-start vs warm-start bring-up | whether the link that comes up on a cold board still comes up on a hot one | after `L5-THERM-02`, POR and immediately re-bring-up while the boards are warm; compare time-to-`cal_done` and `cal_resweep_ctr` with the cold figure | `L5-THERM-02` | both converge; the delta is **recorded**. A warm board that will not calibrate is a tape-out-relevant finding | S (½ day, piggybacks) |
| `L5-WEDGE-01` | wedge signature capture | makes each wedge produce evidence instead of just a dead board | on `WedgeDetected`: immediately probe the **other** board (which may still be alive) for its full health sample, then POR and, post-recovery, re-read the sticky bits that survive | `L4-HET-02` | a signature record per wedge: which die, which direction, read vs write, last FC delta, which node. Today a wedge yields **nothing** but a timestamp | M (1–2 d) |
| `L5-WEDGE-02` | automated detect → POR → retry | matrix `L5-RECOV-02`, which is `PARTIAL-HOM` (manual only) | wrap `L4-HET-02` in the recovery loop; assert `WedgeDetected` is raised (never an infinite block), per-target POR one board at a time ~8 s apart, retry once on "cable not found", redeploy **both**, re-bring-up | `L5-WEDGE-01`, `--auto-por` | both boards return, ≥3 consecutive cycles. Encodes `RISK-9`'s three fpgahub quirks so an operator never has to remember them | M (2 d) |
| `L5-WEDGE-03` | het vs homogeneous wedge rate | the comparison that says whether *heterogeneity* costs anything | run the identical `L4-HET-02` protocol on `[pair.eth-homogeneous]`, same session, same ribbon, same day | `L4-HET-02` | two rates from one bench day. Without the control the het number is uninterpretable — `RISK-8` says the bench must keep eth↔eth as the control, and this is that, quantified | S (½ day of code; ½ bench day) |
| `L5-WEDGE-04` | throughput and latency with variance | matrix `L5-PERF-01/02`, but honest about the wedge | timed write bursts at the largest N that `L4-HET-02` showed survivable; median + p99; report **bytes/s and the number of runs that wedged getting there** | `L4-HET-02` | numbers published **with** the wedge rate that produced them. A bandwidth figure from a lucky BER window is a lie | S (½ day) |

**Tier 4 total: ~8 engineer-days, ~3 attended bench days (one of which is a
2 h thermal run that can share a session with other read-only work).**

---

### 2.6 Priority order, condensed

| Rank | Work | Bench cost | Gated on | Why here |
|---|---|---|---|---|
| 1 | `L0-BUILD-01/06` | **none** | — | Answers "can this pair work at all" for free |
| 2 | `L2-STRAP-01/02`, `L1-PHYS-01` | ½ day, 1 board | — | Positive controls for F7 on both dies |
| 3 | **The compute rebuild** (§0.1) | none (Vivado) | someone in the compute repo | Unblocks 15+ matrix rows and all of Tiers 2–4 |
| 4 | `L3-CLK-05`, `L3-CLK-01`, `L3-CLK-06` | ½ day, 2 boards | 3 | The het link-up itself, with the ribbon diagnostic in front of it |
| 5 | `L4-HET-01` | ½ day | 4 | Minimum viable "the het pair moved data" |
| 6 | `L4-HET-02/03` + `L5-WEDGE-03` | 1½ days | 5 | The wedge-rate number, with its control |
| 7 | `L5-THERM-01` | 2 h (read-only) | 4 | Flagship FPGA-only result; cheap and safe |
| 8 | `L3-SEQ-01/05`, `L4-HET-04/05/06` | 2 days | 4 | Breadth |
| 9 | `L5-THERM-02/03`, `L5-WEDGE-01/02/04`, `L3-CLK-02/03/04` | 3 days | 6 | Characterisation |

**Grand total: ~35 engineer-days and ~9 bench days**, of which the first two
ranks (≈5 days, ½ bench day) are runnable this week and rank 3 is not this
repo's work.

---

## 3. Bring-up sequence for the first het session

The pair has **never run anywhere** — not on this bench, not on any bench.
Treat the first session as a **debug session, not a formality** (`RISK-8`).

### 3.0 Before you lease anything

Do not skip this. It is free and it is the difference between a productive day
and a lost one.

1. `make test-offline` — L0 green.
2. Run `L0-BUILD-01`. **If it fails, stop.** The het link cannot come up; book a
   single-board session for Tier 1 instead, or run the homogeneous control pair.
3. Run `L0-BUILD-03/04/05/06`. Record every failure; `L0-BUILD-04` is an expected
   `xfail` (§0.2).
4. Resolve `[target.kr260-compute-chiplet]` in **your own** `hetsoc.toml` against
   **your own** built `.hwh`, and verify:
   `resolved=True ps_reaches_d2d=False window=0x400000000`
   (the exact command is in [`ETH_COMPUTE_BRINGUP.md` §1](ETH_COMPUTE_BRINGUP.md)).
5. Confirm **no bare-link tool** is staged on either board (`L1-PROBE-05`). The
   `H1` list is in [`SAFETY.md`](SAFETY.md) §2.
6. Open a terminal on `mapstone-dev` with the single-member POR command ready
   (`SAFETY.md` §5, quirks A and B). **Before** the session, not during it.

### 3.1 The session, in order

Each step has an explicit "stop here if" — the point is that a failure at step
*n* must not be diagnosed by attempting step *n+1*.

| # | Step | Command | Success | **Stop here if** |
|---|---|---|---|---|
| 0 | Lease both | `make lease` | both held | either board in use |
| 1 | **Meter the ribbon, powered off** | human, DMM | continuity BCM_n↔BCM_n on all 18 lanes; pin 1 confirmed at **both** J21 ends; **phys 1, 17 (+3V3) and 2, 4 (+5V) stripped or absent** | any rail conductor present (`H7` — this can damage both boards) or any lane open |
| 2 | Power die_a, then die_b | human, separate barrel jacks | both boot | — |
| 3 | Deploy, **sequential, never concurrent** | `make deploy-pair` | `fpga_manager=operating` both | a deploy failure — redeploy, do not proceed half-loaded (`H5`) |
| 4 | **Single-board aliveness, both** | `make bench-status` | eth boot-ROM = `0x18003C00, 0x08000189, 0x080001CD, 0x080001CF`; compute `L1-PHYS-01` SRAM write/read-back passes | either board silent ⇒ it is already wedged; POR and restart at 3 |
| 5 | **Eth die alone: role lock + self-arm** (`L2-STRAP-01/02`) | eth board only | `ROLE_STATUS` = `0x02`; `OBS_MASK_HS[18]=1`, `[20]=0`, `role_locked=1`; FCSM `0→1` | `role_locked=0` ⇒ the **eth** build lost `SELF_ARM_TRAIN_EN`; this is not a het problem. Stop and check `L0-BUILD-01` |
| 6 | **Ribbon integrity** (`L3-CLK-05`) | both boards, link down | eth `rx_valid` toggles while compute trains | dead `rx_valid` ⇒ ribbon/pinout/`L0-BUILD-05`. **Do not touch the RTL.** Go to §3.2 branch B |
| 7 | Concurrent bring-up, **fresh dies only** | `make bench-bringup` | both FCSM=4, `cal_done=1` | anything else ⇒ §3.2 |
| 8 | Hold check | re-read after 30 s, read-only | FCSM still 4, `lane_fault=0x00` | a drop ⇒ record and stop; that is a new failure mode |
| 9 | **Baseline** (`L3-CLK-06`) | read-only, both dies | JSON written | — |
| 10 | 🔴 **One** cross-die write (`L4-HET-01`) | `I_ACCEPT_WEDGE_RISK=1`, POR terminal open | compute local read of `0x2D00_1000` == `0xC0FFEE01` | a wedge ⇒ POR, redeploy both, and **do not retry more than once** |
| 11 | Post-transfer health | `L3-CLK-06` again | FC deltas zero | any CRC delta ⇒ stop the data plane for the session |
| 12 | Teardown | `make release` | — | — |

**Minimum viable "the het pair is alive" = steps 0–10.** Concretely, and this is
the sentence to put in the report:

> *Two KR260s running **different** chiplet designs reached `FCSM=4` with
> `cal_done=1` bilaterally over a J21 ribbon, and a write issued by the eth die's
> PS into `0x2F00_1000` was read back by the compute die's PS at `0x2D00_1000`
> as `0xC0FFEE01` — verified by a **die-local read on the receiver**, with no
> link traversal on the verdict path.*

That is achievable in a single morning **if F7 is fixed**. If it is not, the
minimum viable demonstration degrades to steps 0–6 plus `L1-PHYS-01`, i.e.
*"both dies are alive, both backdoors deliver, and the ribbon carries training
patterns"* — worth having, and worth stating as such rather than as a failure.

### 3.2 Decision tree for step 7 (the bring-up)

The failure signature is almost always one of six things. Diagnose in this order;
each branch is cheap and rules out the ones below it.

```
Step 7: bringup() reports "link did NOT converge bilaterally"
│
├─ A. Read OBS_MASK_HS 0x4_2E03_2194 and ROLE_STATUS 0x4_2E03_2084 on the ETH die.
│     role_locked == 0 ?
│        YES -> the ETH die never locked. Not a het problem, not a ribbon problem.
│               Check SELF_ARM_TRAIN_EN in the eth build (L0-BUILD-01).
│               [STOP — no amount of ribbon work helps]
│        NO  -> continue.
│
├─ B. Read WLINK_LINK_STATUS 0x4_2E03_0234 on the eth die.
│     [4] rx_valid never toggles, while [3] tx_active == 1 ?
│        YES -> nothing is arriving. In order:
│               1. Is the COMPUTE board actually powered and deployed?
│                  (fpga_manager=operating; L1-PHYS-01 passes)
│               2. Ribbon: reseat, re-meter, confirm pin 1 at BOTH ends.
│               3. Ball map: re-run L0-BUILD-05. A same-orientation pair
│                  (two die_a images) drives two outputs onto every lane — H6.
│               4. Only then suspect the SoC.
│        NO  -> continue.
│
├─ C. Read SWI_LANE_STATUS 0x4_2E03_2108 on the eth die.
│     cal_done == 0, FCSM == 1 ?
│        YES -> training is running but never completes. THIS IS THE EXPECTED
│               F7 SIGNATURE: the compute die's Wlink is held in
│               wlink_por_reset because role_locked == 0, so it never emits a
│               usable training pattern and the eth calibrator never locks.
│               Confirm: OBS_CAL 0x4_2E03_2198 [3:0] cal_state is not S_DONE,
│               and lane_locked 0x2108[7:0] is 0x00 the whole time.
│               [STOP — this is §0.1. Needs the compute rebuild, not bench work]
│        NO  -> continue.
│
├─ D. cal_done == 1 on both, but FCSM != 4 ?
│        -> the LL bootstrap stage. Note B2 in F6_ATTRIBUTION.md: the bootstrap
│           is a NO-OP from cold POR, so a failure here is NOT "the bootstrap
│           did not run". Read llrx_state 0x2108[22:21]:
│             == 2 -> byte-align error (REGISTER_MAP.md:253) -> signal integrity
│             else -> read cr_seen [23] / crack_seen [24] on both dies:
│                     one-sided -> unidirectional link; suspect one ribbon
│                                  direction, and re-check L0-BUILD-05 for the
│                                  data lanes, not just the clock
│                     neither   -> both dies transmitting into nothing
│
├─ E. Both FCSM == 0 after ~4 ms, both role_locked == 1 ?
│        -> this is F6 (autonomous training's swreset never released), which
│           means SOMETHING armed autoneg. Read NEGO_TRAIN_STATUS
│           0x4_2E03_2110: train_ok [0] == 1 with FCSM 0 is the F6 fingerprint.
│           A host CANNOT recover this — the Wlink register file is inside the
│           held reset domain. POR both boards.
│           [STOP — check why NEGO_CFG is non-zero; the shipped reset is 7'h00]
│
└─ F. Everything reads plausible but the link is still down
         -> re-run the HOMOGENEOUS control pair (eth die_a + eth-chiplet-flip,
            [pair.eth-homogeneous]) on the same ribbon, same session.
            Control comes up -> the bench is good; it is the het pair.
            Control also fails -> it is the bench (ribbon, boards, deploy),
                                  and nothing you learn about the het pair
                                  today is trustworthy.
```

**Rule for the whole tree: two failures on the same branch ends the session.**
Not because the problem is unsolvable, but because the next thing you learn will
cost a POR cycle and the one after that will cost the boards.

---

## 4. Application examples

These are **demonstrations, not tests**: small, runnable, and built so a person
in the room can see something happen. The design constraint that shapes all of
them is that the pair is **one-directional by design** — eth originates, compute
receives and reads back through its own window. A demo that pretends otherwise
would be dishonest.

The second constraint is diagnostic value: a demo that only prints "PASS" wastes
the fact that someone is watching. Each of these makes the *mechanism* visible,
so that when it breaks, the way it breaks tells you where.

### `APP-01` — "Hello, other die" (the 90-second demo)

**Shows.** The single most compelling fact: a word typed on one board's console
appears in a *different chiplet design's* memory on another board, and the
receiving board reads it back with its own CPU-free backdoor.

**What it needs.** Steps 0–10 of §3.1. Nothing else. No firmware.

**How it is built.** ~80 lines of Python over the existing API:
`pair.map_peer_to(eth, "shared_sram")` → `pair.peer_write(eth,
eth.target.peer(0x1000), word)` → `pair.read_landed(compute, "shared_sram",
0x1000)`. Print both sides' addresses in full (`0x4_2F00_1000` →
`0x2D00_1000`) so the audience sees the CAM's byte rewrite happen.

**Why it is diagnostic.** Run it once with `CAM_CTRL=0` first: the write goes
nowhere, and the far die's `0x2D` is provably untouched. Then arm the CAM and
it lands. That contrast — the same write, one register apart — is the clearest
possible demonstration that the address translator is real, and it is the
control (`L4-SRAM-06`) that makes the positive result mean something.

**Wedge risk.** HIGH (one peer write). Attended, POR staged. **Keep it to one
write per run** — a demo that loops is a demo that wedges in front of an
audience.

### `APP-02` — "Two dies, one clock story" (the honest link dashboard)

**Shows.** A live, refreshing, side-by-side view of both dies' link state, with
the two things everyone gets wrong called out explicitly.

**What it needs.** Link up. **Read-only — cannot wedge.** This is the demo to
leave running on a second monitor for the whole session.

**How it is built.** `hetsoc health` already returns everything; this is a
`rich`/curses front-end over `pair.health_both()` at 1 Hz, plus the four
registers §6 says to add. Two columns, eth and compute — except the compute
column is mostly **"unreachable (`H2-PSM`)"**, which is itself the point.

**Why it is diagnostic.** It makes three traps visible instead of tribal:
* `lane_locked = 0x00` after training is **healthy**, not broken — show it green.
* `CREDIT_COUNT = 4096` means **idle**, not full — and label it
  *"sideband only — does NOT see the AXI nodes that wedge"*, because a dashboard
  that shows a healthy link right up to the moment the board dies is worse than
  no dashboard.
* The per-node FC panel (7 nodes × CRC / Ack-Nack / TX-FIFO) with the five AXI
  data nodes visually separated as **the recovery-stripped ones**.

**Effort.** S — 1 day. **Highest value-per-day item in this section**, because
every other demo and every L4 run benefits from it.

### `APP-03` — "The doorbell" (cross-die IPC, firmware-free)

**Shows.** A message with a payload crosses the ribbon into a *different SoC's*
mailbox, sets `MSG_VALID`, and latches an interrupt **source** on the receiving
die — all with both dies' CPUs held in their boot gates. It is a working IPC
primitive that needs no firmware on either side.

**What it needs.** `L4-HET-04/05`. Compute's mailbox at `0x2A00_0000`.

**How it is built.** `pair.mailbox_send(eth, [w0, w1, w2, w3])` then
`pair.mailbox_recv(compute)`. Display the CAM rule that makes it work —
**`0x002A2F01`, not the `0x00232F01` that is correct for eth↔eth** — and show
the DECERR you get from the wrong one (`L4-CONF-04`). That side-by-side is the
single best illustration of why the heterogeneous pair needed a target registry
instead of a constant.

**Why it is diagnostic.** `irq_status @ 0x2A00_0028` bit[0] is the receiving
die's interrupt source. Watching it latch proves the cross-die interrupt path
end-to-end **up to the NVIC**, and makes the remaining gap (`G-FW`: delivery to
an ISR) precisely visible rather than hand-waved.

**Wedge risk.** HIGH — five peer writes per message. Budget one message per run.

### `APP-04` — "Watch the eye close" (the thermal demo)

**Shows.** The most *physical* thing this bench can show, and the one nobody can
show in simulation: a link whose sampling point was frozen at bring-up, slowly
drifting as the boards warm up.

**What it needs.** `L5-THERM-01`. **Read-only — safe to run for hours.**

**How it is built.** A 2 h logger sampling `SYNC_DET[31:16]`, `OBS_CAL[19:4]`,
the 7 nodes' CRC and (if available) die temperature every 60 s, rendered as a
single time-series plot at the end: **CRC and sync-detect on one axis,
temperature on the other**. Run it from cold start.

**Why it is diagnostic.** It is the evidence for `RISK-2` and the argument for
the `SWI_FORCE_RECAL` re-cal primitive, in one picture. If the curve is flat,
that is a genuinely good result and worth publishing too. Pair it with
`L3-CLK-04` (forced re-cal) to show the eye being *re-centred* — if that test
survives.

### `APP-05` — "Confinement" (the security demo)

**Shows.** The far die refusing traffic it should refuse. Five CAM rules aimed at
five regions the compute die does not expose to the link — `0x2C`, `0x21`,
`0xA0` (CoreSight), `0x00` (code space), and `0x23` (the **eth** mailbox byte,
which compute genuinely does not decode) — each one DECERRing, with the target
region provably unchanged by a compute-local read afterwards.

**What it needs.** `L4-HET-06`. One POR per sub-case budgeted.

**How it is built.** `program_cam(..., allow_unmapped=True)` — the deliberate
escape hatch, which `L2-CFG-09` proves is otherwise closed. Show the framework
**refusing** each rule first, then the operator explicitly overriding it. The
refusal is as much a part of the demo as the DECERR.

**Why it is diagnostic — and why it is the most valuable demo here.** Inbound
confinement is the **largest untested area in the whole plan**
(`VERIFICATION_PLAN.md` §5.5: *"never tested anywhere, in sim or on silicon"*
until `L0-SIM-08`). It is also the property a chiplet security story rests on.
Demonstrating it on silicon, with a person watching, is worth more than another
throughput number.

**Wedge risk.** HIGH and materially higher than the others: a DECERR is an
unreturned-response class, which is the mechanism the wedge rides. **Run it last
in a session, one case at a time, POR between.**

> **Recommended demo set for a 1-day visit:** `APP-02` running all day on a side
> monitor, `APP-01` as the opener, `APP-03` as the substance, `APP-05` last.
> `APP-04` runs in the background from the moment the link comes up and gives you
> the closing slide.

---

## 5. What is NOT testable on FPGA yet, and why

Stated so nobody spends a session finding out.

### 5.1 Blocked by design — **compute → eth is out of scope, not merely unimplemented**

`ps_m`'s target list deliberately omits `d2d0`/`d2d1`
(`nanosoc_compute_soc.yaml:1106-1123`) — the compute SoC's own **H2-PSM**
down-link safety gate: *no external host mastering off-die without a security
review*. `dap_m` is excluded on the same principle (`:1064`).

This is a **policy decision encoded in RTL**, not an oversight. Consequences:

* The compute PS cannot originate a peer write and **cannot program its own CAM**.
* `L4-SRAM-02`, `L4-SRAM-04`, `L4-MBOX-02`, `L5-SOAK-03` (bidirectional soak) and
  every compute-initiated row are **out of scope for this bench configuration**.
  They are not "PLANNED"; they are refused by the design under test.
* Lifting it requires an RTL change **plus a security review** — not a bench
  decision, and not one this programme should pre-empt.
* Note the second-order effect: since compute cannot originate, its
  `peer_aperture = 0x41` and `peer_aperture_mbox = 0x44` are **never exercised
  in-path** on this pair. `G-ADDR` (the `0x40` vs `0x41` question) therefore
  **cannot be closed on FPGA** by this pairing; it stays a simulation item
  (`L0-SIM-15`).

Also note that "the direction TideLink warns about is the harder one"
(`SIM_PLAN.md` §9b) — so the pair is, by construction, testing the *easier*
direction only. Say that in any coverage claim.

### 5.2 Blocked by `F7` until the compute rebuild

Everything at L3 and above (§0.1). Restated because it is easy to under-read: it
is not that the link is *flaky*, it is that `role_locked` on the compute die has
**no reachable setter**, so `cal_done` cannot assert on either die.

### 5.3 Blocked by `F6` — autonomous bring-up

TideLink's autoneg completes training (`train_ok = 1` at 3.8 ms) and its final
act — a link-layer software reset — drops **both** FCSMs to 0, where they stay.
The Wlink register file is inside the held reset domain, so **there is no
software escape**: the repair write itself hangs
(`F6_ATTRIBUTION.md` §2.1, measured).

Therefore:
* Only the **manual `ROLE_CFG` posture** can be used on this bench.
* Arming `NEGO_CFG` on either die as a workaround for F7 would trade a link that
  cannot start for a link that stops — **do not do it**.
* One genuinely interesting question this raises is a **mixed posture** (eth
  manual, compute autoneg). Nobody has simulated it. It is cheap in
  `sim/het_pair` and expensive on the bench, so it belongs to Sim, not here.
* `L0-SIM-16` (TideChart election over a real link) and `L3-TC-02..08` inherit
  the block, on top of `G-TC`.

### 5.4 Blocked by missing firmware (`G-FW`)

Both dies boot-gate their cores in the PS flow, and the compute die's cores halt
on unprogrammed QSPI. So:

| Blocked | Row | Note |
|---|---|---|
| ISR **delivery** (as opposed to the source latching) | `L4-IRQ-04` | needs mailbox `irq_enable` @ `+0x02C`, the NVIC ISER, and a released core — **and the NVIC bit differs per die** (eth CPU1 IRQ0 vs compute's M4/M0+ split) |
| DMA bulk crossing | `L4-DMA-01` | needs DMAC programming from a released core |
| Ethernet path (M2) and frame relay | `L4-ETH-01/02` | MDIO/MAC/stack — eth-side only, compute has no MAC |
| compute→eth via firmware | `L0-SIM-04/06` | and see §5.2 — firmware alone does **not** unblock it, because the role-lock gate is still closed |

**The one firmware item worth scoping now** is much smaller than `G-FW` as
usually framed: a **compute link bring-up stub** — load a few instructions over
SWD into compute SRAM, release the core via `core_remap_0` (`0x2900_0000`), and
have it write `ROLE_CFG = 0x03`. That is a plausible alternative to §0.1's
option 2, *but only in combination with option 1* — without `SELF_ARM_TRAIN_EN`
the gate stays shut whoever issues the write. Worth recording so nobody scopes
the firmware task expecting it to be sufficient.

### 5.5 Blocked by other named gaps

| Gap | Effect on FPGA | Row |
|---|---|---|
| `G-PTP` | compute's PHC exports **no live time** (`.phc_seconds`/`.phc_nanoseconds` tied 0 on **both** compute links). Cross-die PTP is architecturally impossible in this pair — not slow, impossible | `L4-PTP-01` |
| `G-SEC` | cross-die SWD debug needs the `REMOTE_DBG_EN` gate + inbound firewall. `L4-CONF-02` is the **negative that must keep holding** until then — and `APP-05` demonstrates it | `L4-DBG-01` |
| `G-TC` | election is `FAILED-HOM` (dual-root observed on silicon); both dies default `DEVICE_CLASS = 0x0001`, and compute's `BUILD_NOTES.md:117-120` confirms it is a **TideChart parameter, not a chiplet port** — per-die strapping needs an RTL change | `L3-TC-02/04..08` |
| `G-WEDGE` | L4/L5 can be *run* but never *promoted*. `M-H4` is **defined** by this fix | all L4/L5 |

### 5.6 Not blocked, but not testable *on FPGA*

Worth separating, because these are permanently sim-side:
* **Error injection** (`L0-SIM-18`) — you cannot inject a controlled bit error on
  a real ribbon. FPGA gives you the *natural* error rate (`L5-THERM-*`), which is
  a different and complementary measurement.
* **The exact wedge mechanism** — FPGA shows the symptom (bus hang); only sim can
  show `fe_rx_ptr` failing to advance.
* **`d2d_irq` → NVIC mapping** (`L0-SIM-12`) — needs internal probes.

---

## 6. Instrumentation and observability gaps

The question this section answers: *when the het pair fails on a bench at 4 pm,
what would you need to be able to see?*

### 6.1 Registers that exist in the silicon but not in `hetsoc/regs.py`

All four are RO, in-window, wedge-safe, and directly answer questions the
decision tree in §3.2 asks. Offsets are TLAPB-relative and were cross-checked
against **both** dies' pinned `tidelink/docs/REGISTER_MAP.md` (eth `42da64b`,
compute `74c6777`) — they agree.

| Offset | eth SoC addr | Name | Layout | Why it matters |
|---|---|---|---|---|
| `0x2110` | `0x2E03_2110` | `NEGO_TRAIN_STATUS` | `[0]` train_ok, `[1]` train_fail, `[2]` train_in_progress, `[3]` train_peer_nack, `[7:4]` train_state, `[15:8]` train_peer_lane_locked, `[23:16]` train_peer_lane_fault, `[31:24]` train_local_lane_fault | **The F6 fingerprint** (branch E). Also the only view of the *peer's* lane locks and faults |
| `0x2194` | `0x2E03_2194` | `OBS_MASK_HS` | `[7:0]` peer_tx_lane_mask, `[15:8]` peer_rx_lane_mask, `[16]` mask_hs_local_match, `[17]` mask_hs_local_fail, `[18]` **nego_lock_pending_reg**, `[19]` **mask_hs_match**, `[20]` **mask_hs_gate_open**, `[22:21]` wlink_mask_hs_result | **The F7 instrument.** `[18]=1, [20]=0` is exactly "the W1S landed and the gate never opened". Without this, F7 presents as an unexplained `cal_done=0` |
| `0x2198` | `0x2E03_2198` | `OBS_CAL` | `[3:0]` cal_state, `[19:4]` cal_resweep_ctr, `[20]` live training_mode | The **only** authoritative evidence a `SWI_FORCE_RECAL` was consumed (the W1P is open-loop). `cal_resweep_ctr` is the eye-margin metric `L5-THERM-01` is built on |
| `0x2190` | `0x2E03_2190` | `OBS_OBS_ID` | `0x4F42_0100` | Presence marker — distinguishes "this image has the OBS bank" from "these reads are zeros" |

Plus **`SWI_LANE_STATUS` is under-decoded**. `regs.LaneStatus` decodes 6 of the
14 documented fields. The missing ones are diagnostic:

| Bit | Field | Use |
|---|---|---|
| `[20]` | `a2l_replay_app_valid` | skid-empty vs CDC-stuck — the `RISK-6` false-FULL discriminator |
| `[22:21]` | `llrx_state` | `== 2` ⇒ byte-align error ⇒ **signal integrity**, branch D |
| `[25]/[26]` | is_short_pkt / is_long_pkt | framing |
| `[27]/[28]` | pkt_is_cr_pkt / pkt_is_crack_pkt | live (vs sticky `[23]/[24]`) |
| `[29]` | llrx_valid | receiver actually receiving |
| `[30]` | a2l_fc_replay_link_valid | FCSM 4→5 SEND app-valid gate |
| `[31]` | fe_rx_is_full | FCSM 4→5 SEND credit gate |

> ⚠ **RTL/RDL divergence, recorded in the register map itself:** the RDL
> (`tidelink_regs.rdl:437-470`) documents the **older** packing (`fcsm_state` at
> `[20:17]`, `[31:30]` reserved). The RTL above is authoritative. Any decoder
> generated from the RDL will be wrong.

Also missing: `SWI_FORCE_RECAL` as a named constant (`SWI_TRAINING_MODE` bit[6],
W1P) — §2.2 `L2-STRAP-03`.

**Recommendation `OBS-1`:** extend `hetsoc/regs.py` with the four registers, the
seven `SWI_LANE_STATUS` bits, and `SWI_FORCE_RECAL`; add them to
`health.link_health()` and to `APP-02`'s dashboard. **~1 day, no hardware, and
it directly shortens the §3.2 decision tree.** Highest-value instrumentation item
here.

### 6.2 The compute die is nearly dark, and that is the biggest gap

Because of `H2-PSM`, the entire compute-side link view — `SWI_LANE_STATUS`,
`ROLE_STATUS`, `OBS_*`, the FC nodes, the CAM, TideChart — is **unreachable from
the PS**. On the compute die the bench can see exactly two things:

1. `shared_sram_0` @ `0x2D00_0000` and `ipc_mailbox_0` @ `0x2A00_0000` (via
   `ps_m`) — i.e. **the payload, and nothing about how it got there**;
2. two LEDs: `link_active_o_0` → led0, `role_is_master_o_0` → led1
   (`BUILD_NOTES.md:26`).

So on a het failure you have **full telemetry on one die and a blinking light on
the other**. Every diagnosis in §3.2 is written from the eth side for that
reason.

| Rec | What | Effort | Value |
|---|---|---|---|
| `OBS-2` | Fixing C1 (§0.1 rank 2) also **restores the entire compute telemetry plane**. Frame it that way when arguing for it — it is not just "the CAM becomes writable", it is "the compute die stops being a black box" | 2 lines + rebuild | ★ highest |
| `OBS-3` | Until then: **use the LEDs deliberately.** `L1-PHYS-05` records the mapping; photograph or video them during bring-up. It is crude and it is the only compute-side link indication that exists | trivial | M |
| `OBS-4` | Surface a small set of link status bits to a PL-visible AXI-GPIO in the compute BD, bypassing `ps_m` entirely. `BUILD_NOTES.md:114-116` already flags that **only `tidechart_irq` reaches the chiplet boundary** — this is the same gap. A read-only GPIO carries no down-link security concern, so it should not need the `H2-PSM` review | S–M, needs a rebuild | H — pairs naturally with the §0.1 rebuild |

### 6.3 ILA

There is **no ILA** in either chiplet target's block design, and the wedge's
whole signature (`fe_rx_ptr` stalling, `fe_rx_is_full` latching) is internal to
the Wlink FC nodes.

| Rec | What | Effort | Honest assessment |
|---|---|---|---|
| `OBS-5` | An ILA on the **eth** die's D2D AHB (`hsel_peer`, `haddr`, `hwrite`, `hready`, `hresp`) + the FC nodes' `fe_rx_ptr`/`fe_rx_is_full`, triggered on `hready` low for > N cycles | **L — 1–2 weeks**, and it perturbs timing on a design that already has residual RX setup violations | The only thing that would show the wedge as it happens. But the sim already root-caused it (`CROSS_DIE_WEDGE_ROOTCAUSE.md`), so an ILA would mostly **confirm** a known answer. **Recommend deferring** until `G-WEDGE`'s fix lands and needs validating |
| `OBS-6` | A far cheaper substitute: **`L5-WEDGE-01`** — on `WedgeDetected`, probe the *surviving* board immediately. In every observed wedge at least one board answered for a while | S | Do this first |

### 6.4 Log capture and session provenance

| Gap | Rec | Effort |
|---|---|---|
| Board-side kernel/dmesg is never captured. A wedge produces no kernel message, but the moments *before* one may | `OBS-7`: stream `dmesg -w` from both boards into `build/results/<session>/` for the whole session | S |
| No session manifest. When a result is 3 weeks old, nobody can reconstruct **which** bitstreams produced it | `OBS-8`: emit a manifest per session — both `.bit` SHA-256s, both `.hwh` `MEMRANGE`s, all repo commits, the `L0-BUILD-*` results, `TIDELINK_PHY_V2` state, ambient temperature, ribbon serial. `OVERNIGHT_REPORT.md` §8 assumption 1 is precisely the failure this prevents | S — **do this before the first session, not after** |
| Health samples are printed, not retained | `OBS-9`: every `link_health()` call appends JSONL to the session directory, so `L5-THERM-*` and `L5-WEDGE-*` are just queries over it rather than bespoke loggers | S |
| No ribbon identity | `OBS-10`: physically label and serial-number the ribbon; record it in the manifest. Signal-integrity results are meaningless without knowing which loom produced them | trivial |

`OBS-8`, `OBS-9` and `OBS-1` together are **~2 engineer-days and no hardware**,
and they are what turns a bench session from an anecdote into data.

---

## 7. Summary of effort

| Tier | Engineer-days | Bench days | Gated on |
|---|---:|---:|---|
| 0 — offline build assertions | 4 | 0 | — |
| Instrumentation (`OBS-1`, `OBS-8/9`) | 2 | 0 | — |
| 1 — single board | 5 | 1 (1 board) | — |
| **compute rebuild (§0.1)** | *(not this repo)* | 0 | compute repo owner |
| 2 — two boards, control plane | 12 | 2.5 | rebuild |
| 3 — data plane (attended) | 6 | 2 | Tier 2 |
| 4 — soak / characterisation | 8 | 3 | Tier 3 |
| Application examples | 4 | shares Tier 3/4 sessions | Tier 3 |
| **Total** | **~41** | **~8.5** | |

**What one bench session can realistically achieve.** A first het session that
gets through §3.1 steps 0–11 — link up, one transfer, health baselines both
sides — is a **good day**. Add `L3-CLK-05/06` and `L5-THERM-01` (which runs in
the background) and it is an excellent one. Anyone planning to also complete
`L4-HET-02`'s wedge-rate sweep in the same session is planning for a bench that
does not wedge, and this one does.

---

*A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
license. Copyright 2026, SoC Labs (www.soclabs.org).*
