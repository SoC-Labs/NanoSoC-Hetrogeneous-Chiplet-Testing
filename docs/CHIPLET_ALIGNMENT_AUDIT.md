# Chiplet alignment audit — eth ↔ compute heterogeneous pair

**Date:** 2026-08-05
**Scope:** architectural and implementation alignment of the two chiplet designs that must
work together as a heterogeneous die-to-die pair.
**Method:** RTL and `sys_desc` YAML read in preference to prose docs; every load-bearing
claim carries a `file:line`. Build artefacts (manifests, timing reports, XDC, block-design
TCL) read directly. Nothing was modified in either chiplet repo.

**Revisions audited**

| Thing | Revision | Note |
|---|---|---|
| eth chiplet (working copy) | `c432c2f` | `v0.1.1-84-gc432c2f` |
| eth chiplet (vendored `deps/eth-chiplet`) | `7fb7c37` | ancestor, 22 commits behind |
| compute chiplet (working copy) | `d4833be` | `v2026.07.16-chiplet-verified-38-gd4833be` |
| compute chiplet (vendored `deps/compute-chiplet`) | `1a9ab1b` | ancestor, 28 commits behind |
| eth `tidelink` | gitlink pins `3962919`, **checkout is `42da64b`** | 3 commits ahead of the pin |
| compute `tidelink` | `74c6777` | matches gitlink |
| eth `tidechart` | `6cf269d` | matches gitlink |
| compute `tidechart` | `f7bc745` | matches gitlink |

---

## 0. Verdict

**Can these two dies work as a pair today?**

| Direction | In simulation | On a two-board FPGA bench |
|---|---|---|
| **eth → compute** (SRAM + mailbox) | **YES — passing** | **NO** — role-strap / ball-map collision (§2.1) |
| **compute → eth** | not attempted | **NO** — compute PS cannot program its own CAM or role (§2.2); needs firmware |
| **Link bring-up to FCSM=4** | YES, manual `ROLE_CFG` posture only | **NO** — same collision as above |

The two designs are **architecturally compatible**: the D2D pin lists match, the TideLink
revisions are wire-compatible, the decoders are the same parameterised RTL, and the address
asymmetries are all handled by the host registry. The blockers are **not** architectural —
they are four concrete integration defects plus one deliberate policy:

1. **Neither compute KR260 image can pair with the eth die_a image.** Each compute image
   combines one role's ball map with the *other* role's role strap (§2.1). This is new since
   `BRINGUP_GAPS.md` G6 was written and is the single hard stop.
2. **The compute PS backdoor deliberately cannot reach the D2D window** (H2). Because of
   that, defect 1 cannot be corrected in software the way the eth die's would be — and
   neither can defect 3.
3. **The deskew SYNC anchor does not latch by default.** On the eth pair, measured
   2026-08-04, `reanchored = 0` on both dies after a clean bring-up; the link reads FCSM=4 and
   looks healthy but cross-die words never reassemble. There is a proven host workaround
   (R2) — which the compute die cannot execute, because of defect 2.
4. **None of the four shipped bitstreams contains the AXI-data-node recovery fixes** (Fix G,
   header-ECC SEC) — they exist only in the V2 flist, and the images were built from the V1
   flist or from a revision predating the fixes (§2.5). G12 is open on *both* dies.

Everything that has been *proven* about this pair was proven in simulation, against the
**vendored** submodule pins, with **both dies on the V1 PHY**, using the **manual `ROLE_CFG`
posture** — not autonegotiation (§1.4).

**Neither chiplet is fabricated silicon.** Where these repos say "silicon" they mean the
two-board KR260 FPGA pair (`docs/CHIPLET_ALIGNMENT.md:53`). See the note in §1.3.

---

## 1. Side-by-side architectural state

### 1.1 What is in each die

| Dimension | **Ethernet chiplet** | **Compute chiplet** |
|---|---|---|
| Cores | CPU0 "network core" + CPU1 "chip core" (Cortex-M0+ family) | Cortex-M0+ "manager" + Cortex-M4F "compute" (M4 has `BB_PRESENT=1`) |
| Shared SRAM | `shared_sram_0` @ `0x2D000000` (`nanosoc_multicore_soc.yaml:2169`) | `shared_sram_0` @ `0x2D000000` (`nanosoc_compute_soc.yaml:1013`) |
| IPC mailbox | `ipc_mailbox_0` @ `0x2300_0000` (`:2167`) | `ipc_mailbox_0` @ **`0x2A00_0000`** (`:994`) |
| PHC / PTP | full PHC, exports live time to the D2D servo (`:336-338`) | `phc_0` @ `0x2B000000`, *"free-running; no servo/MAC to sync against"* (`:1012`) |
| Ethernet MAC | yes (`rmii_ref_clk` 50 MHz in the KR260 build) | none |
| D2D links | **1** | **2** (`d2d0`, `d2d1`) |
| D2D outbound window | `0x2E000000..0x2FFFFFFF` (32 MB) (`:301`) | `d2d0` `0x40000000` +256 MB (`:1022`); `d2d1` `0x60000000` +256 MB (`:1023`) |
| Peer aperture(s) | `0x2F` (single) | `0x41`+`0x44` (link 0), `0x61`+`0x64` (link 1) |
| Inbound target set | `shared_sram_0`, `ipc_mailbox_0` — exactly two (`:2383-2387`) | `shared_sram_0`, `ipc_mailbox_0` — exactly two (`:1096-1105`) |
| PS backdoor | `eth_ss_0`, initiator `eth_ss_m` — **includes `d2d`** (`:2232`) | `ps_ahb_s` (`:104`), initiator `ps_m` — **excludes `d2d0`/`d2d1`** (`:1110-1123`) |
| D2D interrupts | `d2d_irq[15:0]`, `[7:0]`→CPU0 NVIC, `[15:8]`→CPU1 NVIC (`:316`) | `d2d0_irq`/`d2d1_irq[15:0]`, `[7:0]`→M4 NVIC, `[15:8]`→M0+ NVIC (`:150-151`) |
| TideChart | `NUM_PORTS=1` | `NUM_PORTS=2`, link-1 instance tied off |

### 1.2 D2D pin list — **ALIGNED**

Structurally identical per link; compute suffixes `_0`/`_1`. `NUM_PHY_LANES = 8` on both
(`nanosoc_eth_chiplet.sv:42`, `nanosoc_compute_chiplet.sv:65`).

| Signal | eth (`nanosoc_eth_chiplet.sv`) | compute (`nanosoc_compute_chiplet.sv`) |
|---|---|---|
| fwd clock out | `pad_clk_tx` `:151` | `pad_clk_tx_0` `:137` / `pad_clk_tx_1` `:170` |
| data out | `pad_tx[7:0]` `:152` | `pad_tx_0[7:0]` `:138` / `pad_tx_1[7:0]` `:171` |
| fwd clock in | `pad_clk_rx` `:153` | `pad_clk_rx_0` `:139` / `pad_clk_rx_1` `:172` |
| data in | `pad_rx[7:0]` `:154` | `pad_rx_0[7:0]` `:140` / `pad_rx_1[7:0]` `:173` |
| role strap | `role_strap_i` `:167` | `role_strap_i_0` `:151` / `role_strap_i_1` `:184` |
| reset out | `d2d_reset_o` `:207` | `d2d_reset_o_0` `:160` / `d2d_reset_o_1` `:193` |

Both KR260 builds run the ribbon at the same rate: forwarded clock 319.857 ns / 3.126 MHz,
RX clock 320.000 ns / 3.125 MHz (compute timing report Clock Summary `:196-198`; eth
equivalent). **Aligned.**

### 1.3 Maturity

| | eth chiplet | compute chiplet |
|---|---|---|
| RTL | mature | mature |
| Unit sim | extensive `verif/` | `verif/` + G16 gates (`Makefile:67,81,87,95`) |
| Pair sim | homogeneous proven | homogeneous proven; **het pair** in this repo (§1.4) |
| FPGA (KR260) | `kr260-eth-chiplet{,-flip}`, rebuilt **2026-08-05** | `kr260-compute-chiplet{,-flip}`, built **2026-07-31 / 08-02** |
| ASIC | Genus→Innovus→route, DRC, power/fill — **a `pnr_all` run was live during this audit**; `ASIC/genus-innovus/outputs/` is **empty**, no chip GDS yet | **further along** — padring, bond pads, fill/seal-ring, DRC disposition, **Calibre LVS** (`d4833be`) |
| Fabricated silicon | **NONE** | **NONE** |

> **Terminology trap — correct this before reading any other doc.** "Silicon" throughout
> both repos means **the two-board KR260 FPGA pair**, not a fabricated part.
> `docs/CHIPLET_ALIGNMENT.md:53` states it plainly: *"in both repos means the KR260 FPGA —
> **neither is fabricated ASIC**."* Confirmed structurally: the eth chiplet's
> `ASIC/genus-innovus/outputs/` and `reports/` are empty, and the only `*.gds*` files in the
> repo are ROM via fragments (`ASIC/romlibs/{eth_rom,cc_rom}/`). Phrases like "PROVEN ON
> SILICON 2026-07-27" (`targets.py:676`) and the `docs/*SILICON*` reports all describe FPGA
> bring-up. Nothing in this audit is a statement about a fabricated die.

**The compute ASIC push has not diverged the RTL from what the FPGA flow builds.** The
28-commit delta `1a9ab1b..d4833be` is ASIC-flow collateral (padring, destub, LVS, fill) plus
three RTL/verif commits; the D2D-relevant RTL changes (`efd5039`, `11d1dec`) are in the
*working copy* and are absent from the *vendored pin* — see §2.6. The ASIC flists select
destubbed cores and ASIC SRAM macros, which is expected and is not an FPGA/ASIC RTL fork of
the D2D path.

### 1.4 What has actually been proven, and under what conditions

`sim/het_pair/results.xml` (2026-07-30 08:07) records **10 test cases, zero failures**,
including `test_manual_peer_write_eth_to_compute_sram`,
`test_manual_mailbox_uses_compute_byte`, `test_manual_eth_mailbox_byte_is_confined_on_compute`
and `test_manual_cam_disabled_is_identity`.

Four qualifiers that materially limit what that proves:

1. **Every passing case is from `test_het_manual.py`** — the manual `ROLE_CFG` posture. No
   autonegotiation case passes (F6, §2.4).
2. **The sim compiles the vendored pins**, not the working copies:
   `sim/het_pair/build/switches.f` and `cmp_tidelink.f`/`eth_tidelink.f` reference
   `deps/eth-chiplet/...` and `deps/compute-chiplet/...` throughout.
3. **Both dies compile the V1 PHY** — `deps/*/tidelink/src/rtl/local_overrides/WavD2DGpio*.v`
   plus `deps/*/tidelink/deps/tidelink-gpio-phy/rtl/*`; `TIDELINK_PHY_V2` appears in no
   flist in `sim/het_pair/build/`.
4. **The vendored compute die has only ONE peer aperture.** At `1a9ab1b`,
   `deps/compute-chiplet/src/rtl/chiplet_d2d_decode.sv:176` is
   `wire a_peer = (haddr[31:24] == PEER_BYTE);` — no `NUM_PEER_BYTES` parameter exists, and
   the instantiations pass only `WINDOW_BASE`
   (`deps/compute-chiplet/src/rtl/nanosoc_compute_chiplet.sv:525-526, 554-555`). The second
   source aperture has **never been simulated**.

---

## 2. Alignment matrix

Legend: **A** = aligned · **M** = misaligned (defect) · **D** = asymmetric by design.

| # | Dimension | Verdict | Evidence |
|---|---|---|---|
| 1 | D2D pin list, widths, directions | **A** | §1.2 |
| 2 | Lane count (`NUM_PHY_LANES=8`) | **A** | `nanosoc_eth_chiplet.sv:42`; `nanosoc_compute_chiplet.sv:65` |
| 3 | Ribbon clock rate (3.125/3.126 MHz) | **A** | both timing reports, Clock Summary |
| 4 | D2D window geometry | **D** | eth 32 MB @`0x2E`; compute 2× 256 MB @`0x40`/`0x60`. Each is die-local; the same parameterised decoder serves both (`chiplet_d2d_decode.sv:104` `WINDOW_BASE`) |
| 5 | D2D window decode | **A** | same RTL, `WINDOW_BASE`-parameterised; full-top-byte compare, no aliasing (`chiplet_d2d_decode.sv:49-56`) |
| 6 | Peer aperture count | **D** | eth 1 (`0x2F`); compute 4-slot run per link (`NUM_PEER_BYTES(4)`, `nanosoc_compute_chiplet.sv:527,557`) with `0x41`/`0x44` live and `0x42`/`0x43` dead |
| 7 | Mailbox aperture byte | **D** | `0x44` not `0x42` — `0x42000000-0x43FFFFFF` is the M4 peripheral bit-band alias. Deliberate; commit `11d1dec` |
| 8 | Inbound target set (exactly two) | **A** | eth `:2383-2387`; compute `:1096-1105` |
| 9 | Inbound `shared_sram_0` byte | **A** | `0x2D` both |
| 10 | Inbound `ipc_mailbox_0` byte | **D** | eth `0x23` / compute `0x2A` — kept out of the M4 SRAM bit-band (`nanosoc_compute_soc.yaml:990-993`). Handled by the per-direction CAM |
| 11 | CAM semantics (`[0]`en `[15:8]`match `[23:16]`replace) | **A** | `regs.py:306-331`; same `tl_addr_trans_cam.sv` both sides |
| 12 | TideLink register map | **A** | shared offsets, `regs.py`; `tidelink_top` port lists identical |
| 13 | TideLink wire protocol | **A** | framing, CAM, CRC, FCSM state encodings identical between `74c6777` and `42da64b` |
| 14 | `NEGO_CFG_RESET` | **A** | `7'h00` both (`tidelink_top.sv:141` eth / `:131` compute) |
| 15 | `ROLE_FROM_STRAP` | **A** | `1'b1` both (`:224` / `:214`) |
| 16 | PHY V1/V2 *parameter* | **A** | `USE_PHY_V2 = 1'b0` both (`:94` / `:84`) |
| 17 | PHY V1/V2 *as built* | **M** | compute's two images differ from each other: non-flip **V1**, flip **V2** (§2.3) |
| 18 | `SELF_ARM_TRAIN_EN` | **M** | eth overrides to `1'b1` (`nanosoc_eth_chiplet.sv:615`); compute takes the `1'b0` default (`nanosoc_compute_chiplet.sv:698,899`) |
| 19 | `AUTO_ANCHOR_EN` | **M** | eth `1'b1` (`:615`); the parameter **does not exist** in compute's TideLink revision |
| 20 | `TXGEN_PRESENT` | **D** | eth `1'b0` (`:615`); compute takes `1'b1` default (`tidelink_top.sv:112`). Die-local test aperture |
| 21 | FCSM override set as built | **M** | Fix G and header-ECC SEC in **none** of the four images (§2.5) |
| 22 | Role strap ↔ ball map pairing | **M — HARD STOP** | §2.1 |
| 23 | PS backdoor reaches D2D | **D** | eth yes (`:2232`); compute no (`:1110-1123`) — deliberate, §2.2 |
| 24 | TideChart identity/role | **M** | both claim `DEVICE_CLASS = 16'h0001`; `ELECTION_TIMEOUT` 4096 (eth) vs 256 (compute); `device_strap` unwired on eth |
| 25 | TideChart revision | **M** | branches genuinely diverged — each carries a fix the other lacks |
| 26 | Clocking / reset ordering | **M** | compute D2D TX path times zero endpoints; 8921 no-clock pins in its RX PHY (§2.7) |
| 27 | PTP / PHC servo interface | **D** | compute's short `phc_ahb` variant has no live-time output; documented deviation (`nanosoc_compute_soc.yaml:161-168`) |
| 28 | D2D interrupt map | **D** | same 16-bit split convention; compute surfaces only `tidechart_irq` at the boundary (`kr260-compute-chiplet/tidelink_design.tcl:184-189`) |
| 29 | F6 autoneg reset behaviour | **A** (both broken) | `wlink_por_reset = ~poresetn \| ~role_locked` — eth `axi_chiplet_controller.sv:3039`, compute `:2960` |
| 30 | Vendored pin vs working copy | **M** | §2.6 |

### 2.1 HARD STOP — role strap is paired with the wrong ball map on both compute images

The eth die establishes the convention. Verified in the XDC and block-design TCL:

| Image | `pad_clk_tx` | `pad_clk_rx` | strap `CONST_VAL` | role |
|---|---|---|---|---|
| `kr260-eth-chiplet` | **AD15** | AC14 | **0** (`tidelink_design.tcl:140`) | die_a (master) |
| `kr260-eth-chiplet-flip` | **AC14** | AD15 | **1** (`:140`) | die_b (slave) |

So **die_a transmits on AD15; die_b transmits on AC14.**

Now the compute images:

| Image | `pad_clk_tx_0` | `pad_clk_rx_0` | ball-map role | strap `CONST_VAL` | strap role | consistent? |
|---|---|---|---|---|---|---|
| `kr260-compute-chiplet` | **AC14** | AD15 | **die_b** | **0** (`tidelink_design.tcl:159`) | **die_a** | **NO** |
| `kr260-compute-chiplet-flip` | **AD15** | AC14 | **die_a** | **1** (`:159`) | **die_b** | **NO** |

The ball map is not ambiguous. `kr260-compute-chiplet/kr260_compute_chiplet_tidelink.xdc:25-26`
says verbatim:

> *Balls per BOARD_WIRING S3.2 **die_b column**, which is the mirror of the eth die_a map
> (kr260-eth-chiplet).*

and `:32` labels the TX clock `pad_clk_tx_0 -> AC14  (b->a fwd clock)`. Yet
`tidelink_design.tcl:155-156` says *"Link 0 die_a default 0; the flip target overrides
CONST_VAL to 1"* and sets `CONST_VAL {0}`. The flip target's comment at `:160` is explicit
the other way: *"die_b (FLIP): role strap defaults to 1 (vs die_a 0)"*.

`flows/deploy_pair.sh:59-66` already caught the ball-map half of this and defaults
`COMPUTE_TARGET_B=kr260-compute-chiplet` (`:68`) — correct for the ribbon. But it did not
catch that the same image straps role 0.

**Consequence.** For the intended pair `eth kr260-eth-chiplet` (die_a) + compute die_b:

* Choose `kr260-compute-chiplet` (ribbon correct): **both dies strap role 0**. With
  `ROLE_FROM_STRAP = 1'b1` the strap is terminal, so both boot believing they are master and
  the role never resolves.
* Choose `kr260-compute-chiplet-flip` (role correct): **both dies transmit on AD15** — two
  drivers on every ribbon conductor, the exact hazard `pair.py:69-72` warns about.

**Neither compute image is usable against the eth die_a image.** A correct image needs die_b
balls **and** strap 1, and no such build exists.

**Why software cannot rescue it.** The bench's normal mechanism is the software role lock —
`ROLE_CFG` bit[0] role, bit[1] lock (`regs.py:116`), written by `pair.py:208`. On the compute
die `ROLE_CFG` lives at `0x4003_2080`, inside the `d2d0` window, which `ps_m` does not
reach (§2.2). The compute die's role is therefore fixed by its strap and correctable only by
rebuilding the bitstream — or from compute-side **firmware**, which can reach `d2d0`
(`nanosoc_compute_soc.yaml:1044-1045`).

This refines `BRINGUP_GAPS.md` G6, which asked for a strap driver on the compute side. One
now exists; it is wired to the wrong constant for its ball map.

### 2.2 Asymmetric by design — the compute PS is receive-only (H2)

Verified from both sides.

**eth — PS reaches the D2D window.** `nanosoc_multicore_soc.yaml:2230-2232`:

```
            # D2D: CPU0 is the network core, so it owns the link's data plane
            # (peer aperture + TX/RX FIFO windows) as well as its config regs.
            - name: d2d
```

**compute — PS does not.** `nanosoc_compute_soc.yaml:1106-1123`:

```
        # PS host backdoor initiator (external host -> this SoC). Same passthrough
        # idiom as d2d{0,1}_m, but reaches the FULL functional map (not the narrow
        # D2D grant). EXCLUDES d2d0/d2d1 (no external host mastering off-die without a
        # security review) and the PPB debug windows, mirroring dap_m's exclusions.
        - name: ps_m
          passthrough: ps_ahb_s
          targets:
            - name: qspi_flash_0
            ... 11 targets, none of them d2d0/d2d1
```

**This is a deliberate security posture, not a defect.** It is correctly modelled:
`targets.py:798` sets `ps_reaches_d2d=False`, `targets.py:328-353` refuses D2D addresses
loudly rather than returning the misleading `0x00000000`, and `targets.py:966-972` carries
the flag through TOML overrides. That machinery is sound and should be kept.

The consequence is a real capability limit, and it compounds §2.1:

* the compute PS **can** read/write compute SRAM and the mailbox — the receive side works;
* the compute PS **cannot** program the CAM, set `ROLE_CFG`, read `SWI_LANE_STATUS`, or
  originate a peer write.

So **compute → eth requires firmware on the compute die**, and so does any correction of the
role strap. That is the same conclusion `docs/ETH_COMPUTE_BRINGUP.md` and compute commit
`99da090` ("firmware-driven compute↔compute two-board test runbook (Path A)") reached.

### 2.3 Misaligned — the compute pair is split across two PHY versions

Read from the build manifests:

| Image | `source_commit` | `phy_marker` | flist | built |
|---|---|---|---|---|
| `kr260-eth-chiplet` | `3962919`-**dirty** | **V1** | `tidelink_fpga.flist` | 2026-08-05T09:32Z |
| `kr260-eth-chiplet-flip` | `3962919`-**dirty** | **V1** | `tidelink_fpga.flist` | 2026-08-05T09:34Z |
| `kr260-compute-chiplet` | `a60d581`-**dirty** | **V1** | `tidelink_fpga.flist` | 2026-07-31T14:09Z |
| `kr260-compute-chiplet-flip` | `74c6777`-**dirty** | **V2** | `tidelink_fpga_v2.flist` | 2026-08-02T13:47Z |

Three observations:

1. **All four are `git_dirty: true`.** No shipped image corresponds to a clean committed
   tree. Reproducibility is not currently available for any of them.
2. **The compute pair is internally inconsistent** — non-flip V1, flip V2, and from two
   different TideLink commits. V1 and V2 are not a cosmetic difference: the V2 flist header
   (`flists/tidelink_fpga_v2.flist:3-19`) describes a serdes/deskew/calibrator swap and
   states the two trees *"CANNOT co-compile"*. A **compute ↔ compute** pair is therefore
   V1↔V2 across the ribbon.
3. **The het pairing is safe on this axis.** eth (V1) + `kr260-compute-chiplet` (V1) match.
   The V1 flists are byte-identical between the two repos (`diff` of
   `nanosoc-ethernet-chiplet/tidelink/flists/tidelink_fpga.flist` against the compute copy
   returns nothing).

> **Do not infer the PHY version from the packaged IP tree.** The eth chiplet's
> `tidelink/imp/fpga/eth_chiplet_ip/src/` currently holds a **V2-only** file set
> (`WavD2DGpio_v2.v`, `WlinkGPIOPHY_v2.v`, `tidelink_lane_deskew_v2.sv`,
> `tidelink_phy_align_calibrator_v2.sv`) — but it was rewritten at **12:08 today, 1 h 36 min
> *after* the 10:32 bitstream**, by the **ASIC** flow, for which V2 is the ship configuration.
> The FPGA image on disk is V1, per its own paired manifest (written 10:32:33, same minute as
> the `.bit`). `tidelink/fpga/filelist.tcl:42-43` confirms the selector: V1 is the default and
> V2 requires `TIDELINK_PHY_V2=1` in the environment. **FPGA ships V1; ASIC ships V2.**

### 2.4 F6 — present in both dies, and the eth side compensates one-directionally

The mechanism is identical in both TideLink revisions:

```verilog
wire wlink_por_reset = ~poresetn | ~role_locked;
```

eth `tidelink/src/rtl/local_overrides/axi_chiplet_controller.sv:3039`, compute
`tidelink/src/rtl/local_overrides/axi_chiplet_controller.sv:2960`. The Wlink LL/FCSM is held
in reset until `role_locked` asserts. Autonegotiation is meant to assert it via the mask
handshake; when it does not converge, the FCSM never leaves state 1. The manual `ROLE_CFG`
W1S latches `role_locked` directly and reaches FCSM = 4 = `LINK_IDLE`
(`FC.scala:38-47`).

**Neither revision fixes it**, so the F6 attribution to TideLink stands and the handover is
still the right artefact. Two related asymmetries:

* `SELF_ARM_TRAIN_EN`: eth `1'b1` (`nanosoc_eth_chiplet.sv:615`), compute default `1'b0`.
  The two dies enter training by different paths.
* `AUTO_ANCHOR_EN`: eth `1'b1`; the parameter does not exist in compute's revision, and
  `AUTO_ANCHOR` appears nowhere in the compute chiplet RTL. Re-anchoring is **eth-driven
  only**. The `3962919` mutual-anchor fix helps here — post-fix the eth die keeps beaconing
  after it has itself anchored, which is exactly what a non-fixed peer needs — but there is
  no symmetric path, so the eth die's beacon window is the only thing correcting the compute
  die's RX deskew.

### 2.5 Misaligned — no shipped bitstream has the AXI-data-node recovery fixes

`regs.py:180-199` and `BRINGUP_GAPS.md` G12 describe the wedge: the five AXI data-plane FCSM
nodes ship the upstream, recovery-stripped logic; only the sideband node keeps the SoC-Labs
recovery. This audit confirms it and extends it to **all four images**.

The fixes live only in the **V2** flist:

| Source | V1 flist (`tidelink_fpga.flist`) | V2 flist (`tidelink_fpga_v2.flist`) |
|---|---|---|
| `WlinkEccSyndrome.v` | upstream **bypass**, `:179` | `local_overrides/`, `:243` |
| `WlinkGenericFCSM{,_1..4}.v` | upstream, `:209-213` | `local_overrides/`, `:292-296` |
| `WlinkGenericFCSM_6.v` | `local_overrides/`, `:221` | `local_overrides/`, `:304` |

Therefore:

* **eth and eth-flip** were built with `tidelink_fpga.flist` → upstream ECC bypass, upstream
  FCSM 0–5. **No Fix G, no ECC SEC.** The fixed files exist on disk
  (`src/rtl/local_overrides/WlinkGenericFCSM.v:1007`, `local_overrides/WlinkEccSyndrome.v`)
  but the V1 flist does not reference them.
* **compute** was built with the same V1 flist. Same result.
* **compute-flip** used the V2 flist, but at TideLink `74c6777` the local overrides predate
  the fixes: `grep "state == 3'h4 || state == 3'h5"` in
  `NanoSoC-Compute-Chiplet/tidelink/src/rtl/local_overrides/WlinkGenericFCSM.v` returns
  nothing, and `src/rtl/local_overrides/WlinkEccSyndrome.v` does not exist there — its V2
  flist line `:243` points at the upstream bypass.

**Correction to the prevailing narrative.** The recovery gap is often described as an
eth-has-it / compute-lacks-it asymmetry. On the actual bench images it is **symmetric and
absent on both dies**. That is better news for pairing symmetry and worse news for G12.

### 2.6 Misaligned — vendored pins do not match the working copies

`deps/eth-chiplet` is pinned at `7fb7c37` (22 commits behind `c432c2f`) and
`deps/compute-chiplet` at `1a9ab1b` (28 behind `d4833be`). Both are ancestors, so nothing is
forked — but the delta is not inert:

* The compute vendored pin **predates `efd5039` and `11d1dec`**, so it has **no second peer
  aperture at all** (§1.4 item 4). The registry's `peer_aperture_mbox=0x44` and the
  `NUM_PEER_BYTES=4` reasoning in `targets.py:777-793` describe RTL the sim never compiles.
* The eth vendored pin predates the tidelink bump to `3962919` (AUTO_ANCHOR mutual-anchor
  fix), which the shipped eth bitstream *does* contain.

Separately, the eth **tidelink gitlink is stale against its own checkout**: the parent pins
`3962919` but the working tree is `42da64b`, three commits ahead (`git submodule status`
shows the `+` marker). A fresh `git clone --recursive` of the eth chiplet reproduces
different RTL from what is on disk and from what built the bitstream.

### 2.7 Misaligned — the two builds do not apply the same timing contract to the shared interface

| Image | WNS | TNS / failing | WHS | THS / failing |
|---|---|---|---|---|
| `kr260-compute-chiplet` | **+12.304** | 0.000 / 0 | +0.010 | 0.000 / 0 |
| `kr260-compute-chiplet-flip` | **+13.358** | 0.000 / 0 | +0.010 | 0.000 / 0 |
| `kr260-eth-chiplet` | **−2.708** | −10.834 / **4** | **−22.579** | −180.379 / **8** |
| `kr260-eth-chiplet-flip` | **−3.060** | −12.236 / **4** | **−22.312** | −178.361 / **8** |

The compute figure quoted throughout the docs (+12.30 ns) is real but **does not cover the
D2D interface**. In `kr260-compute-chiplet/tidelink_design_wrapper_timing_summary_routed.rpt`
the generated clock `pad_clk_tx_0_fwd` is defined in the Clock Summary (`:198`) and then
appears **nowhere else in the report** — not in the Intra Clock Table, not in the Inter Clock
Table, not in any path group. **Zero endpoints are timed on the compute D2D transmit path.**
`check_timing` also reports **44275** unconstrained internal endpoints and, under `no_clock`,
**8921** register pins whose root clock is
`u_tidelink_0/u_chiplet_controller/u_wlink/phy/gpio/gpiorx_0/g_t3a_passthru.count_reg[3]/Q` —
i.e. most of the D2D receive recovery datapath is unanalysed.

The eth build *does* constrain it — `set_output_delay -min -20.000` on `pad_tx[*]` against
`pad_clk_tx_fwd` — and fails: all 8 hold violations are `pad_tx[*]` output ports sourced from
`.../phy/gpio/gpiotx_N/g_pad_iob.io_pad_q_reg/C` (report `:10383` onward). The 4 setup
violations are a different, unrelated issue: an `**async_default**` recovery check from
`clk_out1` (25.011 MHz) into `rmii_ref_clk` (50 MHz) on
`u_rmii_to_mii/rstn_shift_reg[0]/CLR` (`:14052`) — an Ethernet-side CDC missing a
`set_false_path`, not a D2D problem.

Whether the eth pad violations are *real* is a separate question — a −20 ns min output delay
on a 320 ns bit period is an aggressive contract and the link is proven on silicon. The
alignment finding is narrower and firm: **the two builds do not hold the shared D2D interface
to the same timing contract**, so their WNS numbers are not comparable and the compute
number does not mean the compute D2D interface has been checked.

### 2.8 Misaligned — TideChart cannot elect a deterministic root across this pair

* Both dies claim `DEVICE_CLASS = 16'h0001` — eth's parameter default
  (`nanosoc_eth_chiplet.sv:47`, passed through at `:816`), and compute does not override the
  shim default at all (`grep DEVICE_CLASS nanosoc_compute_chiplet.sv` returns nothing).
  A tie is guaranteed.
* The tiebreak is computed differently: eth uses `{device_strap, lfsr_r[7:0]}` (the I6
  dual-root fix), compute uses the full 16-bit LFSR. The I6 guarantee does not hold across a
  mixed pair.
* Worse, eth's `device_strap` is **not connected** — `src/rtl/tidechart_shim.sv` in the eth
  chiplet has **zero** references to it, so even the eth side falls back to LFSR entropy and
  the I6 fix is dead code.
* `ELECTION_TIMEOUT` defaults differ: **4096** on eth
  (`tidechart/src/rtl/tidechart_election_fsm.sv:43`, rationale in
  `tidechart/src/rdl/tidechart_regs.rdl:108` — *"Raised from 256: the silicon link RTT is
  materially larger…"*) vs **256** on compute
  (`tidechart/src/rdl/tidechart_regs.rdl:99`, `ELECTION_TIMEOUT[16] = 256`). The compute die
  can settle before the eth die's claim arrives → **dual root**.
* The two TideChart branches have genuinely diverged: eth carries the I6–I9 election
  hardening, compute carries the `f7bc745` enum uplink-port fix. Neither has both.

Note this only matters once TideChart election is exercised; the bring-up path in use pins
roles explicitly and does not depend on it. `targets.py:810` already records that compute
link 1 has no TideChart.

### 2.9 The second source aperture is modelled but not wired

`targets.py` implements it fully — `peer_aperture_mbox` (`:170`), `_peer_source_byte`
(`:414`), `_peer_byte_set` (`:429`), `cam_rule_for` (`:556`). **No runtime path uses any of
it.** `cam_rule_for` and `peer_aperture_mbox` appear only in `tests/test_l0_addressing.py`
and `host/tests_unit/test_targets.py`. In the live path:

* `pair.py:366` — `map_peer_to` takes `match = board.target.peer_aperture`, always aperture #1;
* `pair.py:318` — `program_cam` *rejects* any match byte that is not `peer_aperture`, so
  aperture #2 cannot be programmed even deliberately;
* `pair.py:345` — only `CAM_RULE_0` is ever written; the intended RULE_0=SRAM / RULE_1=mbox
  split does not exist;
* `pair.py:445` — `mailbox_send` addresses via `src.target.peer(...)` with no `which`, so it
  defaults to `shared_sram` and aperture #1.

The result is self-consistent and correct (both CAM rule and address use aperture #1), so
nothing is broken — but the concurrency the second aperture exists to provide is not
available, and the feature is untested against RTL that implements it (§2.6).

**And on the eth side it is not a host-software limitation at all — it is a hard RTL
constraint.** `docs/PEER_APERTURE_PROGRAMMING.md:230-232`:

> *a single 16 MB peer aperture cannot reach two disjoint remote 16 MB regions through this
> CAM. Die A can map its `0x2F` aperture to **EITHER** `shared_sram_0` (`0x2D`) **OR**
> `ipc_mailbox_0` (`0x23`) — **not both at once**.*

The other seven CAM rules cannot help: the whole aperture normalises to one `addr_upper`
value, so only the rule matching `0x2F` ever fires (`:234-240`), and the `base_offset` borrow
trick cannot split it (`:241-249`). A second eth aperture is an RTL respin, and the eth SoC
matrix is 16/16 full (`nanosoc_multicore_soc.yaml:295-297`).

**Consequence for the het pair: eth → compute can carry bulk SRAM traffic or mailbox
doorbells, but not both without reprogramming the CAM between them.** `pair.py:434-449`
`mailbox_send` does exactly that reprogram, so it is correct but it *tears down* the SRAM
mapping each time. This is the asymmetry the compute die's second aperture was introduced to
escape — and the eth die, which is the only die that can currently originate, does not have
it.

---

## 3. What still needs implementing

### 3.1 Compute chiplet

| Item | Blocks | Evidence | Size |
|---|---|---|---|
| **C-1. Rebuild the compute KR260 images so strap matches ball map** — die_b balls + strap 1, die_a balls + strap 0 | **everything on a het bench** | §2.1 | **S** — one `CONST_VAL` per target + rebuild |
| **C-2. Decide and document the compute role mechanism** — with `ps_m` excluding d2d, the strap is the only PS-visible lever; firmware is the only runtime one | C-1 correctness | §2.1, §2.2 | **S** (decision) |
| **C-3. Rebuild both compute images from one TideLink revision and one PHY version** | compute↔compute pairing; reproducibility | §2.3 | **S** |
| **C-4. Constrain the D2D pads in the compute XDC** — `pad_clk_tx_0_fwd` currently times zero endpoints | link reliability; G15 | §2.7 | **M** |
| **C-5. Bump compute TideLink onto the eth line** — costs 2 FPGA-only commits, gains Fix G / ECC SEC / DRAIN once those are in a used flist | G12 on the compute side | §2.5 | **S** |
| **C-6. Fix stale comments in `chiplet_d2d_decode.sv:72-75,111-114`** — still say `NUM_PEER_BYTES=2` / mailbox `0x42`; `11d1dec` changed the instantiations to 4 and did not update the header | operator error | direct read | **S** |
| **C-7. Firmware to originate compute→eth** — set `ROLE_CFG`, program the CAM, issue peer writes | compute→eth entirely | §2.2 | **M** |

### 3.2 Ethernet chiplet

| Item | Blocks | Evidence | Size |
|---|---|---|---|
| **E-1. Commit the tidelink gitlink bump** — parent pins `3962919`, tree is `42da64b` | reproducibility; a recursive clone builds different RTL | §2.6 | **S** |
| **E-2. Decide whether the KR260 image should use the V2 flist** — Fix G and ECC SEC are unreachable from V1 | G12 on the eth side | §2.5 | **M** |
| **E-3. `set_false_path` the `clk_out1 → rmii_ref_clk` reset recovery** — 4 setup violations, Ethernet-side CDC | build hygiene | §2.7 | **S** |
| **E-4. Resolve or waive the `pad_tx[*]` hold violations** — −22.6 ns against `set_output_delay -min -20` | confidence in the D2D contract | §2.7 | **S–M** |
| **E-5. Wire `device_strap` in `tidechart_shim.sv`** — currently zero references, so the I6 dual-root fix is dead code | deterministic election | §2.8 | **S** |
| **E-6. Surface `DEVICE_CLASS` per die in the KR260 targets** — the chiplet parameter exists (`nanosoc_eth_chiplet.sv:47`) but nothing overrides it; both eth dies ship `0x0001` | dual root | §2.8 | **S** |
| **E-7. Land the deskew anchor in RTL** — the `reanchored=0` default currently needs a host poke on both dies (R2). `AUTO_ANCHOR_EN` is in the tree; upstream review says *adopt with changes* and prefers `EPOCH_ANCHOR_EN=1` | every cross-die transfer | R2 | **M** — TideLink jointly |

### 3.3 TideLink / TideChart

| Item | Blocks | Evidence | Size |
|---|---|---|---|
| **T-1. F6 — autoneg leaves the Wlink in reset** | autonegotiated bring-up on both dies | §2.4 | **M** — handover written, unsent |
| **T-2. G12 — AXI-data-node recovery into a flist the FPGA builds use** | cross-die data plane on both dies | §2.5 | **M** |
| **T-3. Reconcile the two TideChart branches** | deterministic root election | §2.8 | **S–M** |
| **T-4. Align `ELECTION_TIMEOUT` and give the dies distinct `DEVICE_CLASS`** | dual-root risk | §2.8 | **S** |
| **T-5. Isolated-write data loss** — a peer write followed by bus-IDLE delivers `0x00000000`. Suspects: XHB500 `xhb500_ahb_to_axi_bridge_chiplet_slv` W-channel capture, `ahb_sub` data-phase handling, `AXI4ToWlink.v` W-beat packetisation | **every mailbox doorbell**, both directions | R3; `NanoSoC-Compute-Chiplet/docs/TIDELINK_ISOLATED_WRITE_DATA_LOSS.md:25-35,106-113` | **M** — `cb33c9f` is in both dies and did **not** fix it |

### 3.4 This test repo

| Item | Blocks | Evidence | Size |
|---|---|---|---|
| **H-1. Add a strap/ball-map consistency check to `flows/deploy_pair.sh`** — it already knows the ball-map inversion (`:59-66`); make it also assert the strap | repeating §2.1 on the bench | §2.1 | **S** |
| **H-2. Wire the second source aperture into `pair.py`** — `map_peer_to`/`program_cam` should use `cam_rule_for` and program RULE_0/RULE_1 | concurrent SRAM+mailbox | §2.9 | **S** |
| **H-3. Bump `deps/compute-chiplet` past `11d1dec`** so the sim exercises `NUM_PEER_BYTES=4` | the second aperture is unsimulated | §2.6 | **S** |
| **H-4. Add a compute deploy action to `fpgahub/fpgahub.toml`** — only `deploy_eth_die_a`/`_die_b` exist (`:45-46,75,93`) | lab automation for the het pair | direct read | **S** |
| **H-5. Rewrite the stale header block in `targets.py:704-741`** | it contradicts its own body | §4 | **S** |
| **H-6. Record the "proven under these conditions" caveats** next to the sim result | over-reading a green suite | §1.4 | **S** |
| **H-7. Add a het-sim case for the mailbox DOORBELL** (`SLOT0_CTRL = MSG_VALID`, an isolated write). The passing `test_manual_mailbox_uses_compute_byte` writes the **data word at offset 0** (observed inbound beat `0x2a000000, 0xc0ffee01, 0`) — it never exercises the `+0x20` doorbell, which is the write the compute repo's own test shows failing | the mailbox path is greener than the evidence supports | R3, §1.4 | **S** |
| **H-8. Resolve the dangling `SECOND_SOURCE_APERTURE.md` citation** — vendor it or make the reference repo-qualified | the spec behind the `0x44` decision is unfindable from this repo | §4 | **S** |
| **H-9. Update `TEST_MATRIX.md`** — 21 rows still `BLOCKED-G-FPGA` on a bitstream that now exists; re-tag against the real blocker (§2.1) | the matrix understates what is testable and misnames what is not | §4 | **S–M** |

### 3.5 Reconciliation against `BRINGUP_GAPS.md`'s 17 items

| Gap | Status | Basis |
|---|---|---|
| **G1** No compute FPGA flow | **CLOSED** | `kr260-compute-chiplet{,-flip}` built 2026-07-31 / 08-02 |
| **G2** No compute PS-backdoor port | **CLOSED as scoped; re-scoped as C1** | `ps_ahb_s` exists (`:104`); its *reach* deliberately excludes d2d (§2.2) |
| **G3** TideLink 297 commits apart | **CLOSED; MIS-SCOPED** | merge-base `351153b`; compute is 2 FPGA-only commits ahead, eth 53. Wire-compatible. The live issue is not commit distance but **flist/PHY variant** (§2.3, §2.5) |
| **G4** D2D window/decode mismatch | **CLOSED** | parameterised `chiplet_d2d_decode` (`WINDOW_BASE` `:104`, `NUM_PEER_BYTES` `:119`) |
| **G5** TideLink APB base differs | **CLOSED** | per-link bases in `targets.py:802-811` |
| **G6** Role strap has no driver on compute | **RE-OPENED, WORSE** | a driver now exists and is wired to the wrong constant for its ball map — §2.1 |
| **G7** `ipc_mailbox_0` byte differs | **CLOSED — asymmetric by design** | per-direction CAM, `targets.py:556-597` |
| **G8** Compute link 1 unterminated | **CLOSED** | tie-offs at `kr260-compute-chiplet/tidelink_design.tcl:161-181` |
| **G9** No compute host tooling | **LARGELY CLOSED** | `hetsoc` carries both targets; gap is now H-4 |
| **G10** fpgahub manifest | **PARTIALLY CLOSED** | manifest binds `kr260_01`/`kr260_02` but only eth deploy actions exist (`:45-46`) → H-4 |
| **G11** TideChart diverged | **OPEN, WORSE** | now divergent in *both* directions, plus `ELECTION_TIMEOUT` 4096 vs 256 (§2.8) |
| **G12** Cross-die data plane wedges | **OPEN** | and confirmed absent from **all four** images, not just compute (§2.5) |
| **G13** No heterogeneous sim harness | **CLOSED** | `sim/het_pair`, 10 passing cases — with the §1.4 caveats |
| **G14** PTP asymmetry | **OPEN — accurately described; root cause is asymmetric by design** | G14's phrasing is **correct**: `nanosoc_compute_chiplet.sv:796-797` and `:995-996` tie TideLink's `phc_nanoseconds`/`phc_seconds` to `30'd0`/`48'd0` on both links, where eth drives them for real (`nanosoc_eth_chiplet.sv:708-709`). The *reason* is deliberate — the compute SoC uses the short `phc_ahb` variant, which has no live-time output to drive them (`nanosoc_compute_soc.yaml:161-168`) — so cross-die PTP is genuinely unavailable, not merely unwired |
| **G15** Clock/reset ordering unanalysed on compute | **OPEN, with hard evidence** | `pad_clk_tx_0_fwd` times zero endpoints; 44275 unconstrained endpoints; 8921 no-clock pins in the RX PHY (§2.7) |
| **G16** No compute regression gate | **CLOSED** | `Makefile:67,81,87,95` — `lint`/`regress`/`cdc`/`elab-strict` |
| **G17** Stale docs | **OPEN, WORSE** | §4 |

**Closed: 9 (G1, G2, G4, G5, G7, G8, G13, G16, and G3 as written).**
**Open: 6 (G6 re-opened, G11, G12, G14, G15, G17).**
**Partial: 2 (G9, G10).**
**Mis-scoped: G3 only** — commit distance is no longer the problem; the live issue is which
flist and PHY variant each image was built from. Every other gap's *description* has held up;
what changed is their status.

Two further notes on the gap list itself:

* **`BRINGUP_GAPS.md` carries no closure markers of any kind.** Grepping it for
  `closed|resolved|done|fixed|landed|superseded` returns nothing relevant. Every gap still
  reads as open, including the nine that are not. The severity emoji (🔴/🟠/🟡/🟢) are the only
  status signal and they were never revised. **This is why the doc reads as far more
  blocking than reality.**
* **`SIM_PLAN.md` F5's requested hard rule was never carried across.** `SIM_PLAN.md:498-502`
  asks that `BRINGUP_GAPS.md` carry "never replay the manual LL bootstrap on a die running
  autonomous negotiation" as a hard rule; `BRINGUP_GAPS.md` contains no F5/F6/`NEGO_CFG` item
  at all. And `F6_ATTRIBUTION.md:410-413` later narrows the rule anyway ("never write the
  register file while the negotiation FSM owns it"). Neither version is recorded in the gap
  list.

The doc's stated critical path — *"G1 → G2 → G3 → G4 → G5; nothing downstream is testable on
a bench until those five land"* — **has been walked**. All five are closed. The critical path
is now **G6 → C-1**, which was rated **S** and sits below four items it now blocks.

---

## 4. Stale documentation

| Doc | Claim | Correction |
|---|---|---|
| `OVERNIGHT_REPORT.md` | the compute KR260 bitstream does not exist | It exists: `kr260-compute-chiplet` (2026-07-31) and `-flip` (2026-08-02), both with `write_bitstream` complete |
| `OVERNIGHT_REPORT.md` | recommends "fixing" C1 | C1 is **deliberate** — `nanosoc_compute_soc.yaml:1106-1109` states the security rationale. The correct response is to *model* it (already done, `targets.py:798`) and route compute-originated traffic through firmware, not to widen `ps_m` |
| `BRINGUP_GAPS.md` G1–G5 | four **HARD STOP**s and a five-item critical path | all closed; the critical path is now G6 |
| `BRINGUP_GAPS.md` G3 | "TideLink pins 297 commits apart" | merge-base `351153b`; compute is **2** FPGA-only commits from it, eth 53. The revisions are wire-compatible |
| `BRINGUP_GAPS.md` G6 | "neither has an FPGA source" for the role strap | both now have an `xlconstant`; the defect is that compute's is paired with the wrong ball map (§2.1) |
| `BRINGUP_GAPS.md` G14 | "compute ties TideLink `phc_seconds`/`nanoseconds` to 0" | The ports are **not tied to 0** — they are deliberately omitted; the short `phc_ahb` variant has no live-time output (`nanosoc_compute_soc.yaml:161-168`) |
| `BRINGUP_GAPS.md` G12 / `regs.py:180-199` | the recovery gap reads as an eth-vs-compute asymmetry | On the shipped images it is **symmetric and absent on both** (§2.5) |
| `targets.py:704-741` | "THERE IS NO KR260 PORT… no `*.bit`/`.hwh`/`.xsa` anywhere" | Contradicted 14 lines later by its own body (`:744-755`) and by the artefacts. The header block is stale and should be rewritten |
| `targets.py:730` | "the compute sims still use 0x40 with the decoder bypassed" | Compute mainline is now `NUM_PEER_BYTES(4)` with `0x41`/`0x44` (`nanosoc_compute_chiplet.sv:527`). Still true of the **vendored pin**, which has no second aperture at all |
| `chiplet_d2d_decode.sv:72-75, 111-114` | `NUM_PEER_BYTES=2`, compute mailbox at `0x42`/`0x62` | Instantiated value is **4**; mailbox is `0x44`/`0x64` (`nanosoc_compute_chiplet.sv:527,557`). `11d1dec` updated the instantiations but not the module header |
| `flows/deploy_pair.sh:56-57` | compute targets built "WNS +12.30ns" | True but **misleading** — that number excludes the D2D interface entirely (§2.7) |
| **Every doc using the word "silicon"** — `targets.py:676` "PROVEN ON SILICON 2026-07-27", the `docs/*SILICON*` reports, `ARCHITECTURE.md` | reads as a fabricated part | **Neither chiplet is fabricated.** "Silicon" means the two-board KR260 FPGA pair — `docs/CHIPLET_ALIGNMENT.md:53`. The eth `ASIC/genus-innovus/outputs/` is empty and a `pnr_all` run was still live during this audit |
| eth `docs/CHIPLET_ALIGNMENT.md:31` | "This repo now straps `DEVICE_CLASS` at build time … die_a=1, die_b=2 (`tidelink_design.tcl:155`)" | **No such property exists.** The only functional difference between the two eth target TCLs is the role-strap constant at `:140`. Both eth dies still ship `DEVICE_CLASS = 0x0001` → dual root (§2.8) |
| eth `docs/IMPLEMENTATION.md:57` | "This is simulation. There is no silicon and no timing/area/power" | Stale the other way — two KR260 targets ship and an ASIC flow runs |
| `targets.py:777`, `tests/test_l0_addressing.py:731`, `host/tests_unit/test_targets.py:310` | cite `SECOND_SOURCE_APERTURE.md §6.2` as the governing spec | **That file does not exist in this repo.** It lives in the compute repo: `NanoSoC-Compute-Chiplet/docs/SECOND_SOURCE_APERTURE.md` (30 KB, 2026-07-30). Either vendor a copy or make the citation repo-qualified — as written it is a dangling reference to the spec behind the `0x44` decision |
| `TEST_MATRIX.md:77` | "the **eight** `PROVEN-SIM-HET` rows" | The table now has **nine** (`:140,141,142,144,146,147,149,152,156`) — `L0-SIM-01` was legitimately promoted by `c8a0f6c` and the prose was not updated |
| `TEST_MATRIX.md` / `TEST_ID_MAP.md` | 21 rows still tagged `BLOCKED-G-FPGA`; `L0-SIM-04/06` still `BLOCKED-G-FW` | The blocking artefact (compute bitstream) now exists. **No matrix row was updated after 2026-07-31.** The real blocker for those rows is now §2.1, not the absence of a bitstream |
| `OVERNIGHT_REPORT.md:121-133` | status distribution across 139 rows | **Sums to 118, not 139.** 21 rows unaccounted for; per-status counts also disagree with the current matrix (BLOCKED-G-FPGA 15 vs 21, PROVEN-HOM 13 vs 20) |
| `docs/BOARD_WIRING.md` / any doc pairing compute images with roles | — | must be re-checked against §2.1 before a bench session |

---

## 5. Risk register — het pair on a real two-board bench

Ordered by likelihood × cost.

| # | Risk | Likelihood | Cost | Mitigation |
|---|---|---|---|---|
| **R1** | **Link never comes up: both dies strap master, or two drivers per lane.** Whichever compute image is loaded, one of the two happens (§2.1) | **Certain** | Session lost; two-driver contention is also an electrical stress | Rebuild one compute image with die_b balls + strap 1 (**C-1**). Until then, do not run the pair |
| **R2** | **The deskew SYNC anchor never latches, so cross-die words never reassemble.** Measured on the eth pair 2026-08-04: `EPOCH_STATUS 0x2E03_2140` bit0 `reanchored = 0` on **both** dies after a clean bring-up, `R8 0x2E03_2100 = 0` (no beacon). The link reads FCSM=4 and looks healthy; the peer write simply never lands and the initiator hangs | **High** — it is the default state of the shipped build | Looks exactly like a dead ribbon | **Host workaround, no rebuild** (`docs/DEV_MESSAGE_AXIREC_RECONCILE_2026_08_04.md:23-26`): after bring-up, before any data, on **both** dies — write `0x2E03_2100 = 0x1C`, wait ~0.4 s, write `= 0x00`; `reanchored` latches 0→1. Then 300-/200-beat soaks ran clean. **The compute die cannot do this over its PS backdoor** (§2.2) |
| **R3** | **The mailbox DOORBELL is an isolated write, and isolated D2D writes lose their data.** A peer write not immediately followed by another AHB transfer crosses the link with the **address correct and the write data delivered as `0x00000000`** (`NanoSoC-Compute-Chiplet/docs/TIDELINK_ISOLATED_WRITE_DATA_LOSS.md:25-35`). The discriminator is "followed by another transfer" vs "followed by bus-IDLE". `pair.py:447` makes the doorbell (`SLOT0_CTRL = MSG_VALID`) exactly that — the last write in the sequence | **High** for any mailbox use | Doorbell silently never fires; the receiver sees data but no `MSG_VALID` | The suspected fix `cb33c9f` **is already an ancestor of both dies' TideLink** — and the compute repo's own `verif/g2_soc_peer_aperture` **still fails** (2026-08-01 22:22): *"mailbox DOORBELL landing wrong: `0x2A000020/0x00000000`, expected `.../0x00000001`"*. Interim: a trailing barrier write (adopted in compute firmware, `nanosoc-compute-system` `adcc097`). **The het sim does not cover this** — see below |
| **R4** | **All-zero reads on the compute die misread as a down link.** Any D2D-window read returns `0x00000000`, decoding as `fcsm=0`/`cal_done=0` | High if tooling is bypassed | Hours chasing a healthy ribbon | Already guarded — `targets.py:328-353` refuses these. **Never bypass `to_host()`** |
| **R5** | **Cross-die data-plane wedge (G12).** No shipped image has the recovery fixes; a single bit error on an AXI data node has no recovery path and hangs the PS bus | Moderate, rises with traffic | JTAG POR from another host | `pair.py:486-552` soak with `stop_on_degrade=True`; poll `FC_AXI_DATA_NODES` between transfers; prefer `read_landed` over `peer_read` |
| **R6** | **Compute D2D pads are unconstrained** — zero timed endpoints on `pad_clk_tx_0_fwd`; 8921 no-clock pins in the RX PHY. Marginal behaviour would be silent and temperature-dependent | Moderate | Intermittent, very hard to localise | **C-4**; until then treat compute link margin as unknown and do not tune thresholds against it |
| **R7** | **Bringing up an already-live link desyncs it.** `LL_SWRESET` on a live link wedged die_a on 2026-07-29 | Moderate — operator error | Wedge → JTAG POR | `pair.py:161-195` already refuses unless fresh. Use `verify_link()` |
| **R8** | **Wrong CAM replace byte on the heterogeneous pair.** compute mailbox is `0x2A`, not eth's `0x23`; a hard-coded `0x00232F01` DECERRs and an unreturned response wedges the bus | Low with the framework, high with ad-hoc scripts | Wedge | `pair.py:324-339` refuses structurally. Ban ad-hoc pokes |
| **R9** | **Irreproducible images.** All four bitstreams are `-dirty`; the eth gitlink is 3 commits behind its checkout | Certain (already true) | Cannot reproduce or bisect a bench failure | **E-1**, **C-3**; rebuild from clean trees before any result worth keeping |
| **R10** | **TideChart dual root** if election is ever started — tie on `DEVICE_CLASS`, divergent tiebreak, `ELECTION_TIMEOUT` 4096 vs 256 | Low (not exercised) | Confusing fabric state | Do not start election on the het pair (**T-4**) |
| **R11** | **eth RX deskew is fine, compute RX deskew depends entirely on the eth beacon.** `AUTO_ANCHOR` is eth-only | Moderate | Data mis-framing | Keep the eth die as die_a; do not shorten the idle window before the first peer write |
| **R12** | **Over-reading the green sim suite.** It proves V1↔V1, vendored pins, manual posture, single compute aperture | Moderate | False confidence | **H-6**; re-run after **H-3** |

---

## 6. Things I could not determine

| Question | What would settle it |
|---|---|
| Are the eth `pad_tx[*]` hold violations real, or an over-tight `set_output_delay -min -20`? | The origin of the −20 ns figure in `kr260_eth_chiplet_tidelink_timing.xdc`, against the GPIO-PHY RX sampling window |
| Was the eth KR260 image ever built timing-clean, or has it always failed these paths? | An archived timing report for the on-silicon-proven build; the `.prev-*` snapshots have no retained reports |
| Does the eth `tidechart_shim.sv` elaborate against tidechart `6cf269d`, whose controller interface changed? | An elaboration run of the eth chiplet with its pinned tidechart |
| Which physical J21 ribbon lands on compute link 0 vs link 1 on a two-board bench? | A bench decision; `targets.py:806` records it as unmade. Link 0 carries the TideChart and is the default |
| Whether the F6 root cause is `wlink_por_reset` or the `apb_debug_unlock_i`/`mask_hs_bypass_i` tie-offs | `sim/het_pair/tb_het_pair.sv` read against the `mask_hs_verified_reg` latch in `axi_chiplet_controller.sv` |
| The compute boot-ROM signature (eth's is known and used as an aliveness canary) | A PS read of the compute boot ROM once **C-1** lands; `targets.py:812` records it as unknown |
