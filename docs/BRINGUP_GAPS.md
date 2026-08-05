# Bring-up gaps — what blocks the heterogeneous pair

> ## ⚠️ STATUS REVISED  [2026-08-05] — 9 of the 17 gaps are CLOSED
>
> This list was written 2026-07-29, when neither die had a usable pairing and
> the compute chiplet had no FPGA flow. Much of it has been walked. The full
> reconciliation — which gap is closed, open, partial or mis-scoped, with
> evidence — is in [`CHIPLET_ALIGNMENT_AUDIT.md`](CHIPLET_ALIGNMENT_AUDIT.md);
> the headline:
>
> - **The documented critical path `G1 → G2 → G3 → G4 → G5` has been fully
>   walked.** G1 (compute KR260 bitstream) closed 2026-07-31; G2 (PS backdoor)
>   closed by `ps_ahb_s`; G4 (window/decode) closed by the `WINDOW_BASE`
>   parameterisation; G5 (APB base) resolved to `0x4003_0000`.
> - **The new critical path is `G6 → C-1`** — G6 is *re-opened* and rated **S**.
>   A strap driver now exists; it is wired to the wrong constant.
> - **G3 was mis-scoped.** The two TideLink revisions are *wire-compatible*
>   (identical ports, framing, CAM, CRC, FCSM encodings); the divergence that
>   matters is configuration, not protocol.
> - **G12 is symmetric, not an asymmetry** — no shipped image carries the FC
>   recovery fixes; both dies were built V1.
>
> **Two blockers not in this list at all**, both found 2026-08-05 and both fixed
> by one compute rebuild:
> - **F7** — the compute die has no armed role-lock route, so the link cannot
>   reach FCSM=4. `SELF_ARM_TRAIN_EN` defaults `1'b0` and compute never
>   overrides it; eth does.
> - **H6** — each compute image pairs one role's ball map with the other role's
>   strap. One pairing gives two masters; the other drives two outputs onto every
>   ribbon conductor.
>
> Both are now caught offline by `tests/test_l0_build.py` (`L0-BUILD-01/04/05`).
> **Do not book bench time on the het pair until `L0-BUILD-01` passes.**

**Question:** what must exist before the NanoSoC **Ethernet** Chiplet and the
NanoSoC **Compute** Chiplet can run as a pair on two KR260s?

**Answer, bluntly:** a lot. The het pair is not "nearly there and needs a bench
session" — it needs a **compute-side FPGA flow that does not exist at all**, plus
four address-map and RTL-version mismatches that would silently misroute or wedge
even once it did. This document establishes each gap from the source, orders them
by what unblocks the most, and says who owns it.

> **Method.** Every claim below is evidenced against the two repos as they stand on
> 2026-07-29. Where something does not exist, the text says what was checked. RTL
> and live tooling win over documentation — several docs in both repos are stale
> and are flagged as such (G17).

**Repos compared:**

| | Path |
|---|---|
| ETH | `/home/dam1n19/SoCLabs/nanosoc-ethernet-chiplet/` |
| COMPUTE | `/home/dam1n19/SoCLabs/NanoSoC-Compute-Chiplet/` |

---

## Summary

| # | Gap | Blocks | Size | Owner |
|---|---|---|---|---|
| **G1** | **No compute-chiplet FPGA flow at all** — no `fpga/`, no KR260 target, no block design, no XDC, no bitstream | everything | **L** (3–6 wk) | compute-chiplet integration |
| **G2** | **Compute top has no PS-backdoor port** — no `eth_ss_0` analogue exists to attach one to | G1, all host tooling | **M** (1–2 wk) | compute SoC / `sys_desc` |
| **G3** | **TideLink pins 297 commits apart** — V2 PHY, calibrator and 5 FCSM overrides exist only on the eth side | link interop | **M** (1–3 wk + rebuild) | TideLink team + both integrations |
| **G4** | **D2D window base and sub-decode mismatch** — `0x2E/0x2F` (eth) vs `0x40`/`0x60` 256 MB (compute), same decoder RTL | peer aperture, all addressing | **M** (1–2 wk) | compute SoC + this repo |
| **G5** | **TideLink APB base differs** — `0x2E03_0000` vs `0x4003_0000`/`0x6003_0000`, undocumented | every host script | **S** (days, after G4) | this repo (target registry) |
| **G6** | **Role strap has no driver on compute** — eth ties it, compute bonds it, neither has an FPGA source | link bring-up | **S** (days, part of G1) | compute FPGA build |
| **G7** | **`ipc_mailbox_0` byte differs** — `0x23` (eth) vs `0x2A` (compute) | cross-die mailbox / doorbell IRQ | **S** (days) | this repo (asymmetric CAM) |
| **G8** | **Compute link 1 is unterminated** — 2 TideLinks, one J21 header | any compute board build | **S** (days, part of G1) | compute FPGA build |
| **G9** | **No compute host tooling** — no probe/bringup/xfer equivalents | bench operation | **M** (1–2 wk) | this repo (`hetsoc`) |
| **G10** | **fpgahub has no manifest bound to `kr260_01`/`kr260_02`**, and the ancestor's manifest **no longer loads** | lab automation | **S** (days) | this repo + fpgahub operator |
| **G11** | **TideChart RTL diverged** — `TC_DEVICE_CLASS` RO (eth) vs RW (compute); both default `0x0001` | deterministic root election | **S–M** | TideChart / TideLink team |
| **G12** | **Cross-die data plane wedges** (FCSM 0–4 recovery stripped) | *the existing pair too* | **M** (threshold tune + rebuild) | TideLink team |
| **G13** | **No heterogeneous sim harness or flist** | pre-silicon confidence | **M** (1–2 wk) | this repo (`sim/`) |
| **G14** | **PTP asymmetry** — compute ties TideLink `phc_seconds`/`nanoseconds` to 0 | PTP over D2D only | **M** | compute SoC |
| **G15** | **Clocking / reset ordering unanalysed on compute** | link reliability | **S–M** | compute integration |
| **G16** | **No compute `regress`/`lint`/`cdc` gate** | change confidence | **S** | compute-chiplet repo |
| **G17** | **Stale docs in both repos** contradict the RTL | operator error | **S** | both |

**Critical path: G1 → G2 → G3 → G4 → G5.** Nothing downstream is testable on a
bench until those five land. G12 blocks the *homogeneous* pair today and will
block the het pair identically.

---

## G1 — No compute-chiplet FPGA flow at all 🔴 **HARD STOP**

**What's missing.** Everything needed to put the compute chiplet on a KR260.

| Checked | Result |
|---|---|
| `NanoSoC-Compute-Chiplet/fpga/` | **does not exist** (repo has `ASIC build cdc docs flist nanosoc-compute-system scripts src syn sys_desc tidechart tidelink verif`) |
| `NanoSoC-Compute-Chiplet/constraints/` | **does not exist** (the eth repo has one) |
| `tidelink/imp/fpga/` | contains only `ASIC/` — no `output/`, no `project/`, no IP-packaging area |
| `tidelink/fpga/targets/` | only `mps3` + 14 `pynq-z2-*` — **no `kr260-*`** |
| `tidelink/fpga/Makefile:51` `VALID_TARGETS` | pynq-z2 variants + `mps3` only; `BUILD_GOALS := all build_design package_ip` — no eth-chiplet-style IP-packaging goal |
| `*.bit` / `*.xsa` / `*.hwh` / `*.dtbo` / `*.bd` / `*.xpr` anywhere in the repo | **zero bitstreams**; the 73 hits are all pynq-z2/mps3 XDC |
| `kr260` string in the whole repo | 2 hits, both in the **base SoC** (`nanosoc-compute-system/docs/KR260_IMPLEMENTATION_PLAN.md`, `…/fpga/targets/pynq_kr260`) — neither is a chiplet build |

By contrast the eth side has two complete, dated, manifested bitstreams in
`tidelink/imp/fpga/output/kr260-eth-chiplet{,-flip}/` (`.bit` 7,797,819 B, plus
`.bin`, `.hwh`, `.xsa`, routed `.dcp`, timing report and `tidelink_manifest.json`
recording `source_commit 0ed6d46`, `phy_marker "V2"`, `flist
tidelink_fpga_v2.flist`, built 2026-07-24).

**Why it blocks.** There is nothing to load onto the second board. The het pair
cannot be attempted in any form.

**What must be built.**
1. A `kr260-compute-chiplet` (and `-flip`) target in `tidelink/fpga/targets/`.
2. A Vivado block design: PS8 preset, `M_AXI_HPM0_FPD` → SmartConnect →
   AXI-to-AHB bridge → the SoC's backdoor port (**which does not exist — G2**),
   plus clock/reset association.
3. A J21 XDC for `pad_clk_tx_0` / `pad_tx_0[7:0]` / `pad_clk_rx_0` /
   `pad_rx_0[7:0]`, straight **and** flip ball maps, plus SWD/UART/LEDs.
4. An IP-packaging step (the eth flow's `package_eth_chiplet_ip.tcl` equivalent).
5. `VALID_TARGETS` + Makefile plumbing, and the ZynqMP `.bin` rule
   (`bit2bin_zynqmp.py`, header-strip only — **not** the byte-swapped Zynq-7000
   flavour, which silently corrupts a ZynqMP load).
6. Link-1 tie-off (**G8**) and a role-strap source (**G6**).

**Rough size:** **L — 3–6 engineer-weeks.** The eth equivalent took a full build
cycle plus iteration; expect the same, and expect timing to need work (the eth
build closes at WNS −2.923 ns / WHS −22.408 ns and calls the hold pre-existing).

**Owner:** compute-chiplet integration.

> **Cheapest de-risking available today:** specify the compute FPGA build to use
> the **same J21 ball map with the flip (die_b) assignment** as
> [`BOARD_WIRING.md`](BOARD_WIRING.md) §3.2. Then the ribbon, strip list, ground
> returns and plug-in order all carry over unchanged. Say this **before** the XDC
> is written.

---

## G2 — The compute chiplet has no PS backdoor port 🔴 **HARD STOP**

**What's missing.** The eth chiplet top exposes an AHB **target** port,
`eth_ss_0_{haddr,htrans,hwrite,hsize,hburst,hprot,hwdata,hmastlock,hrdata,hready,hresp}`
(`src/rtl/nanosoc_eth_chiplet.sv:60-70`). That is the entire basis of the PS-side
bench flow. On the built bitstream it lands at PS phys `0x4_0000_0000 ..
0x4_FFFF_FFFF` (`tidelink.hwh:4112`, `MASTERBUSINTERFACE="M_AXI_HPM0_FPD"`,
`SLAVEBUSINTERFACE="eth_ss_0"`), mapping SoC HADDR `A` → `0x4_0000_0000 + A`.

The compute chiplet top (`src/rtl/nanosoc_compute_chiplet.sv:62-189`, 81 ports)
exposes clock/reset, QSPI, UART, PPS, SWJ-DP, DFT and the two link faces. Grepping
it for `eth_ss_0` / `ss_0_` returns **nothing**. **There is no port to attach a
backdoor to.**

**Why it blocks.** Every proven bench capability — boot-ROM aliveness, role
readback, link bring-up, cross-die transfer, health polling — is driven **PS-side
over that backdoor, with no firmware and no SWD probe**. Without it, the only
bring-up route on a compute KR260 is SWD plus on-chip firmware, and no such
firmware exists for the link (**G9**).

**What must be built.**
1. A new AHB target port on `nanosoc_compute_soc`, re-exported through
   `nanosoc_compute_chiplet` — a base-SoC `sys_desc` change, regeneration, and a
   new `sys_desc/chip_boundary/` entry.
2. Its address decode inside the SoC matrix (the eth one reaches the whole SoC
   map, which is what makes the boot-ROM probe possible).
3. The BD segment plus `assign_bd_address`, recorded in the `.hwh`.

> ⚠️ **The eth BD's `assign_bd_address` requests `0x8000_0000` / 1 GB
> (`tidelink_design.tcl:233-234`) and Vivado placed it in the HPM0_FPD *high*
> aperture instead.** Do not trust the TCL; read the built `.hwh`. This exact
> discrepancy is why the bare-link tools poking `0x8403_xxxx` wedge the board.

**Rough size:** **M — 1–2 engineer-weeks**, and it is **upstream of G1**: the BD
cannot be wired until the port exists.

**Owner:** compute SoC / `sys_desc` owner, then compute-chiplet integration.

---

## G3 — TideLink submodule pins are 297 commits apart 🔴 **HARD STOP**

**What's missing.** The two chiplets pin different TideLink revisions, from
**different upstream URLs**:

| | Pin | Remote |
|---|---|---|
| ETH | `884c4a8` (`freeze-2026-07-22-61-g884c4a8`) | `git@github.com:SoC-Labs/TideLink-Chiplet-Interconnect-AHB.git` |
| COMPUTE | `3f3de09` (`v2026.07.16-chiplet-verified`) | `https://git.soton.ac.uk/soclabs/tidelink.git` |

`git rev-list --count 3f3de09..884c4a8` = **297**; the reverse is **0** (compute's
pin is a strict ancestor). `git diff --stat 3f3de09 884c4a8 -- src/rtl/ deps/` =
**34 files, +12,600 / −621**, including:

- `WavD2DGpioRx_v2.v` **+1121 (new)**, `WavD2DGpio_v2.v` +278, `WlinkGPIOPHY_v2.v` +21 — **the V2 GPIO PHY**
- `tidelink_phy_align_calibrator_v2.sv` **+2499 (new)**
- `WlinkGenericFCSM{,_1,_2,_3,_4}.v` **+1298 each (new local overrides)**, `_6` +89
- `axi_chiplet_controller.sv` **+1328**, `tidelink_top.sv` +357, `tidelink_autoneg.sv` +91

**Why it blocks.** These are the PHY, the calibrator and the flow-control state
machines — **the actual wire behaviour between dies**. The eth bitstream was built
`phy_marker: "V2"` / `flist: tidelink_fpga_v2.flist`; the compute repo has never
built V2 for hardware. Two dies from `3f3de09` and `884c4a8` are at best an
unvalidated combination, and the forwarded-clock calibration is exactly the kind
of thing that fails asymmetrically.

**What must be built.** Roll the compute chiplet's TideLink pin forward to the eth
pin (or a common descendant), re-run the compute repo's elaboration and synthesis,
and reconcile the two remotes onto one. The compute `docs/STATUS.md` already flags
"still needs **silicon** validation of V2".

**Rough size:** **M — 1–3 engineer-weeks** plus a compute rebuild. Rolling forward
across 297 commits including new PHY files is not a no-op.

**Owner:** TideLink team (which pin is canonical, and the remote split) plus both
chiplet integrations.

---

## G4 — D2D window base and sub-decode mismatch 🔴 **HARD STOP**

**What's missing.** The two SoCs put their D2D window in different places, and
`chiplet_d2d_decode.sv` — **byte-identical between the two repos** — is hard-wired
to the eth map.

| | ETH | COMPUTE |
|---|---|---|
| Outbound window | `0x2E00_0000`, **32 MB** (`nanosoc_multicore_soc.yaml:2199`) | `d2d0` `0x4000_0000` **256 MB**; `d2d1` `0x6000_0000` 256 MB (`nanosoc_compute_soc.yaml:1017-1018`) |
| Number of TideLinks | 1 | **2** |
| Peer aperture | `0x2F00_0000` (`haddr[24]==1`) | *effectively* `0x4100_0000` / `0x6100_0000` |

The decoder splits on `haddr[24]` and `haddr[19:16]`
(`chiplet_d2d_decode.sv:41-49,113-114,138`). Fed a window based at `0x4000_0000`
(`nanosoc_compute_chiplet.sv:498-502` passes `d2d0_ahb_m_haddr` raw), `haddr[24]`
is **0** across `0x4000_0000–0x40FF_FFFF`, so:

- `0x4000_0000–0x4000_FFFF` → `blk=0` → **`ahb_tx`** (the wedge-prone TX aperture), **not** the peer aperture
- `0x4003_0000` → tlapb; `0x4004_0000` → tcapb
- the peer aperture actually lands at `0x4100_0000`, aliased at `0x43/0x45/…/0x4F` because `haddr[31:25]` is ignored

Grepping the compute repo for `0x41000000`, `0x40030000`, `0x60030000`,
`0x61000000` returns **zero hits**. **The compute chiplet's real sub-decode map has
never been written down.**

**Neither compute test catches this:**
- `verif/chiplet_d2d_decode/tb_tx_gate.sv` drives `0x2E00_0004`, `0x2E01_0000`,
  `0x2F00_0000`, `0x2E03_0000` — **the eth map**, against the compute decoder.
- `verif/g2_soc_peer_aperture/tb_soc_pair.sv` **bypasses the decoder entirely**
  (`:163` wires `d2d0_ahb_m` straight into `tidelink_top.ahb_sub`, `:350` ties
  `ahb_tx_hsel(1'b0)`) and programs `RULE_0 = 0x002D4001` (`0x40 → 0x2D`). In the
  real chiplet, `0x4000_0100` selects `ahb_tx`, not `ahb_sub`.

**Why it blocks.** A peer write aimed at what looks like the aperture lands on the
**TX aperture** instead. Best case it errors; worst case it is exactly the class of
access the eth repo documents as historically wedge-prone
(`docs/D2D_HREADY_LOOP.md`).

**What must be built.** A decision and then the work: either (a) re-base the
compute D2D window onto a `0x2E`-style 32 MB window so the shared decoder is
correct, or (b) parameterise `chiplet_d2d_decode.sv` on the window base and
re-verify **with the decoder in path** on both sides. Then write the map down, and
fix `tb_tx_gate.sv` to drive the compute map.

**Rough size:** **M — 1–2 engineer-weeks**, mostly re-verification.

**Owner:** compute SoC plus whoever owns `chiplet_d2d_decode.sv` — currently
duplicated in both repos, which is itself a hazard.

---

## G5 — The TideLink APB base differs, and is undocumented 🔴

**What's missing.** TideLink's internal register **offsets** are identical
(`sys_desc/tidelink_top.yaml` is **byte-identical** between the repos — `diff`
exit 0, 443 lines; likewise `tidechart.yaml`, 166 lines). So `ROLE_CFG @ +0x2080`,
`ROLE_STATUS @ +0x2084`, `SWI_LANE_STATUS @ +0x2108` and the CAM at
`+0x4000/+0x4004/+0x4010` all carry over — the compute repo's own test uses exactly
those (`verif/g2_soc_peer_aperture/test_soc_peer_aperture.py:48-50`).

**But the base does not.** `0x2E03_0000` on eth; `0x4003_0000` (link 0) /
`0x6003_0000` (link 1) on compute — and, per G4, neither is written down anywhere
in the compute repo. There is no compute `docs/STATUS_REGISTERS.md`; the compute
`docs/` directory holds exactly six files (`D2D_HREADY_LOOP.md`, `G2_PAIR_SIM.md`,
`PEER_APERTURE_PROGRAMMING.md`, `PHYSICAL_HANDOFF.md`, `PIN_POLICY.md`,
`STATUS.md`).

**Why it blocks.** Every eth-side script and document hard-codes `0x2E03_xxxx`.
Pointed at a compute die they address the wrong thing — and on a chiplet board an
out-of-window or wrong-aperture access is the wedge hazard, not an error code.

**What must be built.** This is precisely what this repo's **target descriptor
registry** exists for: `Target.window_base`, a per-target TideLink APB base,
per-target `inbound_targets` and `peer_aperture`, with `to_host()` refusing
anything outside. Once G4 settles the compute map, adding a
`kr260-compute-chiplet` `Target` is a small, well-bounded change — **provided
nothing hard-codes the eth base.**

**Rough size:** **S — days**, after G4. It is the cheapest gap on the list *and*
the one that most repays being designed for now.

**Owner:** this repo (`host/hetsoc/targets.py`).

---

## G6 — The role strap has no driver on the compute side 🟠

**What's missing.** The two chiplets disagree about what `role_strap` even is.

**ETH — tied off in the chip boundary.**
`sys_desc/chip_boundary/nanosoc_eth_chiplet.yaml:213-233`, verbatim:

```
# STRAPS — all three tied. FIRMWARE now owns the per-die role.
#
# role_strap_i sets role_effective at POR (axi_chiplet_controller.sv:575),
# so with it tied 0 BOTH dies boot believing they are master. Firmware must
# role-lock each die before the link is used.
- { soc_port: role_strap_i,       const: "1'b0" }
- { soc_port: mask_hs_bypass_i,   const: "1'b0" }
- { soc_port: apb_debug_unlock_i, const: "1'b0" }
```

**COMPUTE — bonded pads**, per link (`nanosoc_compute_chiplet.yaml:88-94`):
"*These are pads, not ties: the chiplet cannot bring a link up without them and
their values differ between the two dies of a link pair.*"

**On the eth KR260 FPGA build the strap is a Vivado `xlconstant`**
(`targets/kr260-eth-chiplet/tidelink_design.tcl:136-139,196`) — `CONST_VAL {0}`
for die_a, `{1}` for the flip target. A `diff` of the two target TCLs shows exactly
that one line plus a header comment. **It is a build-time constant baked into the
bitstream, not a runtime pin.**

The path actually used on the bench is neither: it is the **software role lock**,
`ROLE_CFG @ 0x2E03_2080` bit[0] = role, bit[1] = lock.

**Why it blocks.** The compute chiplet's `role_strap_i_0` is a real port and a real
pad, but **nothing would drive it on an FPGA build** — no `xlconstant`, no
AXI-GPIO, no XDC. And with the SW path, both sides must agree on which register
sequence pins which role.

**What must be built.** A strap source in the compute BD (mirroring the eth
`xlconstant`), *and* a decision recorded in this repo about which mechanism is
authoritative — strap or `ROLE_CFG`. Do **not** rely on auto-election (G11).

**Rough size:** **S — days**, naturally part of G1.

**Owner:** compute FPGA build; the policy decision belongs to this repo.

---

## G7 — `ipc_mailbox_0` is at a different address 🟠

| Inbound D2D target | ETH | COMPUTE | Agree? |
|---|---|---|---|
| `shared_sram_0` | `0x2D00_0000` | `0x2D00_0000` | ✅ |
| `ipc_mailbox_0` | **`0x2300_0000`** | **`0x2A00_0000`** | ❌ |

Sources: `nanosoc_multicore_soc.yaml:2167,2169,2383-2387` (eth `d2d_m` targets)
and `nanosoc_compute_soc.yaml:989,1008,1091-1100` (compute `d2d0_m` / `d2d1_m`).
Both sides confine the inbound initiator to exactly those two targets; everything
else DECERRs.

**Why it blocks.** The CAM rewrites `addr[31:24]` on the **sender**, so the replace
byte must match **the receiver's** map. Cross-die SRAM works either way (both
`0x2D`). The mailbox does not: **eth→compute must replace with `0x2A`,
compute→eth with `0x23`.** Every eth-side script and doc hard-codes `0x23`.

That mailbox is not a nicety — it is the **general-purpose cross-die interrupt**.
A far-die write latches `irq_status @ 0x2300_0028` (eth) which feeds CPU1 IRQ0, and
it is PS-observable with no firmware. Get the byte wrong and the write DECERRs on
the far die with no local indication.

**What must be built.** Per-direction CAM rules in the target registry, not one
shared constant. The one-aperture-reaches-one-16 MB-region constraint still holds,
so SRAM and mailbox remain mutually exclusive per rule on both sides.

**Rough size:** **S — days.** Free if the registry is written correctly from the
start; expensive to retrofit once `0x23` is baked in.

**Owner:** this repo.

---

## G8 — Compute link 1 is unterminated 🟠

**What's missing.** The compute top mandatorily exposes a **second** TideLink:
`pad_clk_rx_1`, `pad_rx_1[7:0]`, `pad_clk_tx_1`, `pad_tx_1[7:0]`, `user_ref_clk_1`,
`role_strap_i_1`, `mask_hs_bypass_1`, `apb_debug_unlock_1`, `i2c_*_1`, plus a
second `chiplet_d2d_decode` and a second 256 MB window at `0x6000_0000`. Nothing in
the repo ties any of it off.

**Why it blocks.** A KR260 has **one** J21 header. Link 1 has to be safely
terminated and its default-responder path exercised, or a stray access into the
`d2d1` window hangs the matrix — the same failure class as the main wedge.

**What must be built.** Tie-offs in the FPGA wrapper (`pad_rx_1` low,
`pad_clk_rx_1` parked, `role_strap_i_1` defined), and a **negative test** that a
`0x6000_0000`-window access terminates rather than hangs. Add it to the L2 suite.

**Rough size:** **S — days**, part of G1. The *test* is worth writing now.

**Owner:** compute FPGA build, plus this repo for the negative test.

---

## G9 — No compute-side host tooling 🟠

**What's missing.** The eth side has `eth_ss_probe.py`, `kr260_eth_bringup.py`,
`kr260_eth_xfer.py`, `kr260_eth_regress.py` and `kr260_eth_run.sh` — all addressing
through the backdoor and all refusing out-of-window addresses. The compute repo's
`tidelink/pynq_host/scripts/` has 50 files and **zero** equivalents; it carries the
**bare-link** `tl36` / `tl37` / `tl38_bringup` / `tl39` scripts instead — which are
exactly the tools that wedge a chiplet board.

There is also no built compute firmware image for the link. The compute repo has
firmware *sources* under `nanosoc-compute-system/`, but no image, no backdoor (G2),
and no host script.

**Why it blocks.** Nothing can drive the compute die at the bench.

**What must be built.** This is what this repo is *for*: `hetsoc` generalises the
eth scripts into a design-agnostic framework over a target registry. Once G2/G4/G5
land, a compute `Target` is a config entry rather than a new tool.

> 🔴 **Do not solve this by pointing the compute repo's `tl39.py` /
> `tl38_bringup.sh` at a chiplet board.** Those are bare-link tools with a
> bare-link map. See [`SAFETY.md`](SAFETY.md) H1.

**Rough size:** **M — 1–2 engineer-weeks**, largely already scoped as this repo's
core deliverable.

**Owner:** this repo (`host/hetsoc/`).

---

## G10 — fpgahub has no manifest bound, and the ancestor's manifest no longer loads 🟠

**Established live against the daemon on `mapstone-dev`:**

```
GET /api/v1/targets/kr260_01/manifests    -> {"active":null,"default":null,"manifests":[]}
GET /api/v1/targets/kr260_02/manifests    -> {"active":null,"default":null,"manifests":[]}
GET /api/v1/targets/kr260_01/manifest     -> {"detail":"board 'kr260_01' has no manifest bound"}
```

The `action:deploy` / `action:reset` entries in `fpgahub status` history are
**historical dispatches**. The manifest that produced them —
`nanosoc-ethernet-chiplet/tidelink/fpga/fpgahub.toml` — **no longer loads**:

```
action:deploy_pair.command: unknown token namespace 'pair' in {pair.local.role}.
known namespaces: ['artefact','board','chassis','firmware','host','link','manifest','port','script']
```

The `{pair.*}` namespace was **removed** when fpgahub moved to the
board/target/link model; `{link.*}` replaces it. Every `tidelink/fpga/fpgahub.toml`
in the lab fails to load for this reason.

> **Therefore: the ancestor runbook's `fpgahub actions run kr260_01
> deploy_kr260_eth_chiplet_pair` does not work today.** It is documented there as
> `[PROVEN]` because it *was*, before the fpgahub upgrade. Deploy currently has to
> go through `make` directly, or through a manifest this repo supplies.

**Two further constraints, established live:**

1. **There is no `[links.*]` between `kr260_01` and `kr260_02`.** `/api/v1/groups`
   shows each KR260 as its own 2-member *chassis* (`kr260_0N_pl` + `kr260_0N`); no
   link entity joins the two boards. So **`{link.*}` tokens raise** on these
   targets. Roles must be passed as literals, per action.
2. **`kr260_01_pl` / `kr260_02_pl` have `host.ssh = null`.** A `{host.ssh}` token
   on a `_pl` target raises `token {host.ssh} resolved to None`. Bind PS-side
   actions to `kr260_01` / `kr260_02` only.

**What must be built.** The manifest and actions in [`../fpgahub/`](../fpgahub/)
(this repo owns them), plus an **operator edit on `mapstone-dev`** adding the path
to `manifest_paths` in `/etc/fpgahub/config.toml` and reloading. See
[`../fpgahub/README.md`](../fpgahub/README.md).

**Rough size:** **S — days**, split between this repo and one config change.

**Owner:** this repo plus the fpgahub operator on `mapstone-dev`.

---

## G11 — TideChart RTL has diverged 🟡

`sys_desc/tidechart.yaml` is **byte-identical** between the repos, and
`src/rtl/tidechart_shim.sv` is too (both `DEVICE_CLASS = 16'h0001`). **The
submodule RTL is not:**

```
diff -rq nanosoc-ethernet-chiplet/tidechart/src/rtl  NanoSoC-Compute-Chiplet/tidechart/src/rtl
  tidechart_apb_regs.sv     differ
  tidechart_controller.sv   differ
  tidechart_election_fsm.sv differ
  tidechart_enum_fsm.sv     differ
```

The load-bearing difference:

| | ETH (`585e042`) | COMPUTE (`f7bc745`) |
|---|---|---|
| `TC_DEVICE_CLASS` @ `0x10` | **RO**, parameter-sourced (`tidechart_apb_regs.sv:178`, `:563`) | **RW**, resets to the parameter (`:187`, `:482`) |

So the compute die can be given a distinct class at runtime; **the eth die
cannot.** The compute repo's own `docs/STATUS.md` names the RW register as the fix
for a deterministic root — **and that fix is not in the eth build.**

Both dies also instantiate with no `DEVICE_CLASS` override
(`nanosoc_eth_chiplet.sv:797 .NUM_PORTS(1)`, `nanosoc_compute_chiplet.sv:1066
.NUM_PORTS(2)`), so both boot claiming class `0x0001` and the root election is a
coin-flip. Observed on silicon: **dual-root**, each die self-electing with its own
random id; `force_root` (`TC_CTRL[2]`) is decoded and stored but **never
consumed**; `reset` (`TC_CTRL[3]`) does **not** clear `election_done`; the default
`election_timeout` is shorter than the D2D round trip.

**Why it blocks.** Only TideChart-layer work. The **link** does not depend on it —
pin die_a grandmaster via `ROLE_CFG` master-lock and TideChart stays a non-gating
diagnostic. Note it also means the two dies would run **different TideChart RTL**,
which is its own interop question.

**Rough size:** **S–M.** Reconciling the two RTL copies is small; a deterministic
per-die `DEVICE_CLASS` strap needs a rebuild.

**Owner:** TideChart / TideLink team.

---

## G12 — The cross-die data plane wedges 🔴 *(blocks the existing pair too)*

Not a het-specific gap, but it will block the het pair identically and it is the
**top blocker for reliable cross-die operation on the hardware that already
exists**.

The shipped FPGA build resolves the five AXI data-plane FC nodes
(`WlinkGenericFCSM{,_1,_2,_3,_4}` = AW/W/B/AR/R) to the **upstream,
recovery-stripped** copies; only the sideband `FCSM_6` keeps the SoC-Labs recovery
logic. A single bit error or dropped ACK therefore has no recovery path and hangs
the PS AXI bus permanently. Mechanism, evidence and the bounded fix (a threshold
tune of `SOCL_L7_MIN_CRACK_EMITS` for the 40 ns silicon ratio, plus re-pointing
`tidelink_fpga_v2.flist`) are in [`SAFETY.md`](SAFETY.md) H3, eth-chiplet
`docs/CROSS_DIE_WEDGE_ROOTCAUSE.md`, and the upstream request in
`docs/TIDELINK_SILICON_FEEDBACK.md` P1.

**Consequence for this repo:** L4/L5 are attended-only and out of CI, indefinitely.
**Consequence for planning:** even a perfect het bring-up would inherit this. It
should be fixed *before* the het pair exists, so het bring-up is not debugged
through an intermittent wedge.

**Rough size:** **M** — a threshold tune, a flist re-point, validation in the
silicon-faithful sim tier, then a rebuild. **Owner:** TideLink team.

---

## G13 — No heterogeneous simulation harness 🟡

Both pair testbenches are **homogeneous**:

- ETH `verif/g2_soc_pair/tb_g2_soc_pair.sv` — two `nanosoc_multicore_soc`.
- COMPUTE `verif/g2_soc_peer_aperture/tb_soc_pair.sv:175` — one real
  `nanosoc_compute_soc` plus an `ahb_probe_mem` stand-in for die B
  (`:10-20`: "*Die A = REAL `nanosoc_compute_soc` … stand-in @0x2D000000*").

Neither repo can even **compile** both tops in one simulation: the compute repo's
`flist/` has only `nanosoc_compute_chiplet.flist`, the eth repo only the
`nanosoc_eth_chiplet.flist` family. **No flist would produce a mixed build.**

**Why it matters.** G4 (the decode mismatch), G7 (the mailbox byte) and G6 (the
role strap) are *exactly* the class of bug a mixed-top sim catches in an afternoon
and a bench catches by wedging a board. Given G12, **finding them pre-silicon is
worth a great deal.**

**What must be built.** `sim/` in this repo: both chiplet tops instantiated
back-to-back with the D2D pads tied together, driven by cocotb, with a merged
flist. Note this inherits G3 — the two tops currently want different TideLink RTL,
so a mixed build forces that reconciliation, which is arguably a feature.

**Rough size:** **M — 1–2 engineer-weeks.** **Owner:** this repo (`sim/`).

---

## G14 — PTP asymmetry 🟡

`nanosoc_compute_chiplet.sv:49-54` (header comment): the compute SoC uses the
**short `phc_ahb` PHC variant**, whose D2D PHC bundle has **no live-time
`seconds` / `nanoseconds` outputs**, so TideLink's `phc_seconds` /
`phc_nanoseconds` inputs "*have no SoC source and are **tied to 0 here, per
link***". The eth chiplet drives them for real (full PHC plus `ha1588` servo).

**Impact:** PTP-over-D2D will not work in a het pair. The AHB data plane and the
control plane are unaffected. Scope PTP tests as **eth↔eth only** and say so in the
test matrix.

**Rough size:** **M** if PTP over the het link is ever required. **Owner:** compute
SoC.

---

## G15 — Clocking and reset ordering unanalysed on compute 🟡

**Clocking.** The eth ASIC boundary **aliases `user_ref_clk` and `rtc_clk` onto
`sys_fclk`** (`nanosoc_eth_chiplet.yaml:198-199`), documenting the cost: the Wlink
PLL reference is no longer independent of `sys_hclk`, so the link rate is tied to
the system clock and inherits its jitter. Compute keeps `user_ref_clk_0/1` as
separate bonded pads. On the KR260 the eth build derives the PHY divider from
`clk_wiz_0/clk_out1`, and `BUILD_NOTES.md` records that getting this wrong cost
28 ns of WNS. A compute KR260 BD must reproduce that exact relationship; nothing in
the compute repo encodes it. Compute's synthesis SDC assumes `sys_hclk` 20 ns,
`pad_clk_rx_*` 4 ns, `user_ref_clk_*` 10 ns — a different ratio set again.

**Reset ordering.** The eth repo has a full analysis (`docs/RESET_ORDERING.md`)
with the concrete hazard: `role_locked` gates both the recovered-RX-clock reset and
both sides of the a2l ACK-pointer CDC, and latching it on a dead `pad_clk_rx`
produces a **permanent, non-recoverable false-FULL wedge that no simulation can
see**. The compute repo has **no `RESET_ORDERING.md`**; its `PHYSICAL_HANDOFF.md`
§2 says the ordering is "**unanalysed**".

Also relevant to the het pair: `ROLE_CFG` survives `hresetn` but not `poresetn`;
**the CAM survives neither**. A het pair with asymmetric reset sources will
silently drop the CAM on one side and the first peer write will DECERR.

**Rough size:** **S–M.** **Owner:** compute integration — the analysis exists on the
eth side and largely transfers.

---

## G16 — No compute-side regression gate 🟡

| | Make targets | Scripts |
|---|---|---|
| ETH | `bootstrap lint check regress cdc elab-strict chip-boundary chip-wrapper elab asic-flist clean` | `lint.sh`, `regress.sh`, `check_chip_boundary.py` |
| COMPUTE | `bootstrap chip-boundary chip-wrapper elab clean` | `bootstrap.sh`, `check_chip_boundary.py` |

There is no `make regress`, no lint and no CDC gate on the compute side, so none of
the gaps above would be caught by a compute-side change.

**Rough size:** **S** (port the eth scripts). **Owner:** compute-chiplet repo.

---

## G17 — Stale documentation that contradicts the RTL 🟢 *(cheap, and it causes real errors)*

| Doc | Says | Reality |
|---|---|---|
| COMPUTE `docs/PHYSICAL_HANDOFF.md` | "**STUB** … the structural top and the chip-boundary spec **do not exist yet** … What has NOT been verified: **Everything**" | both files exist; the doc is dated 2026-07-10. Its TODO sections (clock domains, reset topology, power intent) are genuinely still unfilled. |
| COMPUTE `docs/PIN_POLICY.md` | tidelink `3f3de09` on a frozen feature branch (2026-07-10) | matches the pin, but the eth side is 297 commits ahead (G3) |
| COMPUTE `docs/STATUS.md` | `nanosoc-compute-system @ 5b94912`, `tidechart @ c0fdf1a` | `git submodule status` says `a0f8599` and `f7bc745` |
| ETH `nanosoc_eth_chiplet.yaml:46` | "DEAD in the pinned tidelink (**3f3de09**)" | that is the *compute* repo's pin; eth is on `884c4a8` |
| ETH `docs/PIN_POLICY.md` | "all five pins are on default branches … risk closed" (2026-07-16) | wrong against the working tree |
| ETH `targets/kr260-eth-chiplet/tidelink_design.tcl:233` | window at `0x8000_0000` | the built bitstream places it at `0x4_0000_0000`. **This one wedges boards.** |
| ETH `docs/KR260_BENCH_RUNBOOK.md` §3 | `fpgahub actions run … deploy_kr260_eth_chiplet_pair` `[PROVEN]` | the manifest no longer loads and nothing is bound (G10) |

**Rough size:** **S.** **Owner:** both repos. The `0x8000_0000` one is a safety
issue, not a tidiness issue.

---

## What *can* be done today

Not everything is blocked. In rough order of value:

| Now | Level | Notes |
|---|---|---|
| **Run the homogeneous eth↔eth pair** | L1–L3 | Fully **[PROVEN]**. It is the control that tells you whether a future het failure is bench or design. Do this first, and keep it green. |
| **Build the framework, registry and guards against the eth target** | L0–L3 | The whole value of G5/G7 is realised by writing the registry correctly *before* a compute target exists. |
| **Write the L2 negative tests** | L2 | Out-of-window refusal, `require_link_up` on a down link, the G8 unterminated-window case. All wedge-free. |
| **Build `sim/` with two *eth* tops** | — | Gets the harness working; swapping one top for compute later is then a flist change (G13). |
| **Land the fpgahub manifest** | — | G10 is small and unblocks lab automation for the homogeneous pair immediately. |
| **Specify the compute J21 ball map to match** | — | Free now, expensive after the XDC is written (G1). |

**Blocked until G1–G5:** any two-board het test, at any level.

---

## A note on "silicon"

Neither chiplet exists as **fabricated ASIC silicon**. "First silicon" in the
eth-chiplet docs means **the KR260 FPGA** — every reported symptom is a PS AXI /
JTAG-POR FPGA symptom.

- **ETH:** two KR260 bitstreams, on a bench, working (link plus data plane). Its
  `ASIC/genus-innovus/outputs/` and `reports/` are **both empty** — it has not been
  synthesised.
- **COMPUTE:** the opposite. RTL complete, VCS-elaborated
  (`build/elab/simv_compute_chiplet`), and **synthesised to a gate netlist**
  (`ASIC/genus-innovus/outputs/nanosoc_compute_chiplet_gate.v`, 34.0 MB,
  2026-07-28; retimed `p05_qor.rep` 2026-07-29, Genus 21.15, WCCOM, **0 violating
  paths**). But no P&R, no GDS, no bitstream and no board flow.

So the two are at **complementary, non-overlapping** stages: one has hardware and
no synthesis, the other has synthesis and no hardware. That is the real shape of
G1, and it is worth stating plainly to anyone planning a bench session.

One outstanding compute-side RTL issue is recorded in `syn/asic_elab/FINDINGS.md`:
"*`nanosoc_compute_chiplet` does NOT elaborate ASIC-clean in Genus … 7 multidriven
combinational leaf pins*", tracing to three flops driven from two `always` blocks
in TideChart's `tidechart_link_state_agent.sv`. The gate netlist post-dates that
document, so it may have been fixed since — **confirm before citing it either
way.**

---

## References

- [`SAFETY.md`](SAFETY.md) · [`BENCH_RUNBOOK.md`](BENCH_RUNBOOK.md) · [`BOARD_WIRING.md`](BOARD_WIRING.md) · [`../fpgahub/README.md`](../fpgahub/README.md)
- ETH: `docs/CROSS_DIE_WEDGE_ROOTCAUSE.md`, `docs/TIDELINK_SILICON_FEEDBACK.md`,
  `docs/STATUS_REGISTERS.md`, `docs/PEER_APERTURE_PROGRAMMING.md`,
  `docs/RESET_ORDERING.md`, `docs/KR260_BENCH_RUNBOOK.md`,
  `sys_desc/chip_boundary/nanosoc_eth_chiplet.yaml`,
  `nanosoc-multicore-system/sys_desc/nanosoc_multicore_soc.yaml`,
  `tidelink/fpga/targets/kr260-eth-chiplet/{tidelink_design.tcl,BUILD_NOTES.md}`
- COMPUTE: `docs/STATUS.md`, `docs/PHYSICAL_HANDOFF.md`, `syn/asic_elab/FINDINGS.md`,
  `sys_desc/chip_boundary/nanosoc_compute_chiplet.yaml`,
  `nanosoc-compute-system/sys_desc/nanosoc_compute_soc.yaml`,
  `src/rtl/nanosoc_compute_chiplet.sv`, `verif/g2_soc_peer_aperture/`
- fpgahub: `/home/dam1n19/SoCLabs/fpgahub/src/fpgahub/manifest.py`,
  `docs/MANIFESTS.md`, `UPGRADING.md`; live daemon on `mapstone-dev:7245`
