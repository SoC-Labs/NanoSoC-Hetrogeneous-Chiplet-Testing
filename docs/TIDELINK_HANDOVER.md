# TideLink — autonomous bring-up leaves the Wlink held in reset (pre-silicon, sim)

**From:** the heterogeneous chiplet D2D bench (`NanoSoC-Hetrogeneous-Chiplet-Testing`),
one `nanosoc_eth_chiplet` die and one `nanosoc_compute_chiplet` die co-simulated
pad-to-pad under VCS, 2026-07.
**To:** the TideLink development agent/team.
**What this is:** a pre-silicon finding in the **autonomous** (`NEGO_CFG = 0x61`,
zero-poke) bring-up path. It is not visible on either chiplet repo's own
testbench, because both of those poke `ROLE_CFG` over APB on both dies and so
never select the autonomous posture.

**TL;DR:** autonomous negotiation *and* training complete correctly —
`ST_TRAIN_DONE`, `train_ok = 1`, `train_fail = 0`, all 8 peer lanes locked — and
then **`WlinkGenericFCSM_6`'s `io_tx_reset` / `io_rx_reset` / `io_app_reset`
remain asserted permanently on both dies.** The FCSM is pinned at state 0 with
`io_app_enable = 1`, the link never comes up, and because the Wlink register file
is inside that held domain, an external APB write to `WL_LINK_ENABLE_RESET` to
recover it **hangs the host bus**. There is no software escape.

The equivalent manual bring-up (`ROLE_CFG` over APB, autoneg parked in
`ST_BYPASS`) reaches cal_done at 89.8 µs and is at FCSM 4 by the first sample
(192.4 µs) — same RTL, same build, same testbench.

Full investigation, with the 2×2 controls that attribute this:
`docs/F6_ATTRIBUTION.md` (in this repo).

---

## 🔴 P1 — after `ST_TRAIN_DONE`, the Wlink link-layer reset never de-asserts

### Symptom

Two dies, autonomous posture, zero pokes on the far die. Measured trace
(`sim/het_pair`, `MODULE=test_f6_longwatch`, logging every autoneg and FCSM
transition for 500,000 cycles past `cal_done`):

| t (µs) | Event |
|---:|---|
| 60.93 | slave autoneg `2 → 5` — loses, parks in `ST_NEGO_DONE` |
| 255.99 | master autoneg `4 → 9` — wins; `role_locked` both dies at 256.4 µs |
| 1661.45 | master `11 → 12` `ST_TRAIN_ENTER` (I²C-writes peer `SWI_TRAINING_MODE := 1`) |
| 2312.91 / 2394.83 | master `12 → 13` `ST_TRAIN_RUN`, `13 → 14` `ST_TRAIN_POLL_PEER` |
| 2485.4 | `cal_done` both dies; both FCSMs at 1; `swi_training_mode_r = 1` on **both** dies |
| 3148.81 | master `14 → 15` `ST_TRAIN_EXIT`; `train_peer_lane_locked_w = 0xFF`, no faults |
| 3797.85 | **slave FCSM `1 → 0`** |
| 3800.27 | master `15 → 16` **`ST_TRAIN_DONE`**, `train_ok_w = 1`, `train_fail_w = 0` |
| 3800.33 | **master FCSM `1 → 0`** |
| 3800 → 12485 | **both FCSMs remain at 0 for the whole remaining 8.7 ms** |

So the negotiation and training sequence you designed **works**. What follows it
does not.

### The stuck signals

Sampled at three points after `train_ok`, pure observation, no DUT writes
(`sim/het_pair/test_f6_late_ll.py`, `TESTCASE=test_f6_observe_after_train_ok`).
Values are `master / slave`:

| net — `<die>.…u_chiplet_controller.u_wlink.tl2wl.wlink_tidelinktl` | 3800.6 µs | 4600.6 µs | 8600.6 µs |
|---|---|---|---|
| `state` | 0 / 0 | 0 / 0 | 0 / 0 |
| `io_app_enable` | **1 / 1** | 1 / 1 | 1 / 1 |
| `en_ff2_tx_demet_io_out` | **0 / 0** | 0 / 0 | 0 / 0 |
| **`io_tx_reset`** | **1 / 1** | **1 / 1** | **1 / 1** |
| **`io_rx_reset`** | **1 / 1** | **1 / 1** | **1 / 1** |
| **`io_app_reset`** | **1 / 1** | **1 / 1** | **1 / 1** |
| `reset` (APB domain) | 0 / 0 | 0 / 0 | 0 / 0 |
| `u_autoneg.swreset_hold_r` | 109 / 0 | **0 / 0** | **0 / 0** |
| `u_autoneg.train_ok_r` | 1 / 0 | 1 / 0 | 1 / 0 |
| `u_autoneg.state_r` | 16 / 5 | 16 / 5 | 16 / 5 |

Reading:

* `io_app_enable = 1`, so **the enable is not the problem**. The FCSM cannot
  leave state 0 because `en_ff2_tx_demet_io_out` — `io_app_enable` 2-flop-synced
  into `io_tx_clk` (`WlinkGenericFCSM_6.v:1131`) — is held low by its own reset.
* The APB-domain `reset` is released; only the **tx / rx / app** resets are held.
* `u_autoneg.swreset_hold_r` has already counted down to **0**, and the FSM is in
  `ST_TRAIN_DONE`. So whatever asserted the Wlink reset is no longer being
  driven by the autoneg FSM's own hold counter, yet the reset is still high
  4.8 ms later.
* It is **not** `wlink_por_reset` (`axi_chiplet_controller.sv:2832
  wire wlink_por_reset = ~poresetn | ~role_locked;`) — `role_locked` is 1 on both
  dies throughout.

That points at the `swi_swreset` / `local_swreset_pulse_w` path —
`axi_chiplet_controller.sv:1339` (`local_swreset_pulse_w`), `:1352-1380`
(`fch_quiesced_r`, "the LL `swi_swreset` (FCCTRL 0x208 bit[3])…"), `:2534`,
`:3340` (`.local_swreset_pulse(local_swreset_pulse_w)`), `:3398-3510` (the
injected `0x00027f08 → 0x00027f00 → …` sequence and `FCH_SWRESET_DWELL`).
`ST_TRAIN_EXIT` sets `swreset_hold_nxt = T_SWRESET_HOLD` immediately before
`ST_TRAIN_DONE` (`tidelink_autoneg.sv`, `ST_TRAIN_EXIT` / `TXN_CHECK` success
arm); that is the pulse that drops the FCSMs from 1 to 0 and which then never
releases.

### Why there is no software workaround

* **Writing `WL_LINK_ENABLE_RESET (0x0208)` after `train_ok` hangs the bus.**
  Measured: the `LL_SWRESET_ON / LL_SWRESET_OFF / LL_ENABLE` triplet issued on
  the master at 3800.6 µs — the first write never completes; the AHB master times
  out after its full 50,000 clock cycles at 4800.65 µs. The Wlink register file
  is inside the held reset domain, so it cannot answer.
* **Clearing `train_auto_en` does not help.** Your own RTL documents
  `NEGO_TRAIN_CFG 0x210C = 0` as the on-silicon escape hatch
  (`axi_chiplet_controller.sv:1278-1281`, citing `td_v2_hwlib.sh` rcp :91).
  Tested two independent ways — an APB write at 84.6 µs, and a register deposit
  at reset mirroring the existing `NEGO_TRAIN_CFG_RESET = 16'h0000`. Both behave
  as specified (the master takes `… → 11 → 5`, correctly declining the
  rendezvous) but **the master's `swi_calibration_done` then never asserts**:

  ```
  cal_done never asserted on both dies within budget.
    master R8_SWI_LANE_STATUS = 0x40020000  (bit16 = cal_done -> 0)
    slave  calibration_done   = 1
  ```

  With `train_auto_en = 0` the autonomous calibration FSM no longer owns the
  per-lane slip (`REGISTER_MAP.md:302-306`) and, on the negotiation winner,
  nothing else drives it to `S_DONE`. So in the autonomous posture **neither**
  setting of `train_auto_en` brings the link up.
* **The manual posture is not available to us.** `nanosoc_compute_chiplet`
  exports no AHB or APB port at all (81 ports; only QSPI, UART and the SWJ-DP,
  and `dap_m` is deliberately not granted the D2D windows). It can *only* be
  brought up autonomously. That is our finding to fix, not yours — but it is why
  this defect is a hard blocker for us rather than an inconvenience.

### Why this is TideLink's and not the integrator's

The 2×2 was run to settle exactly this. All four rows below are the **same**
testbench (`nanosoc-ethernet-chiplet/verif/g2_soc_pair`), the **same** two
identical `nanosoc_eth_chiplet` dies, the **same** TideLink commit `3ed78fe` on
both dies, and the same calibrator sim-bypass. The only variable is the posture:

| Posture | LL bootstrap | FCSM, both dies |
|---|---|---|
| manual `ROLE_CFG` over APB | yes | **4** ✅ |
| manual `ROLE_CFG` over APB | no | **4** ✅ |
| **autonomous `NEGO_CFG = 0x61`** | no | **1**, then 0 ❌ |
| **autonomous `NEGO_CFG = 0x61`** | at `cal_done` | AHB write hangs ❌ |

The heterogeneous pair (two *different* chiplet tops, TideLink `3ed78fe` vs
`3f3de09`) produces a signature **byte-for-byte identical** to row 3.

So, positively eliminated as causes:

* **Heterogeneity and cross-revision interop** — row 3 has neither.
* **The chiplet integration** (SoC matrix, `chiplet_d2d_decode`, `tidelink_top`
  instantiation) — rows 1 and 2 reach FCSM 4 through the identical stack.
* **`tidelink_top` hard-coding `.apb_debug_unlock_i(1'b1)` /
  `.mask_hs_bypass_i(1'b1)`** and ignoring the boundary ports of the same name
  (eth override `src/rtl/local_overrides/tidelink_top.sv:2064-2065`; compute
  upstream `tidelink/src/rtl/tidelink_top.sv:2039-2040`). Identical in *all four*
  rows, so it cannot be the discriminator — and positively confirmed harmless
  here, since the peer-mask exchange and the training rendezvous both complete.
  (Still worth fixing as a wart: two boundary ports are silently dead.)

---

## 🟠 P2 — the chiplets build **V1**; your autonomy proof is **V2**-only

`cocotb/tidelink_top_pair/test_zeropoke_por.py` — the test that proves zero-poke
autonomous bring-up reaches `fcsm = 4` — is documented to run with
`TIDELINK_PHY_V2=1`, i.e. `flists/tidelink_fpga_v2.flist`.

**Both chiplets build the V1 flist** (`flists/tidelink_fpga.flist`, via each
repo's `flist/resolve_tidelink_flist.py`). Verified: no `TIDELINK_PHY_V2` define
appears anywhere in the generated compile — neither in the resolved TideLink
flists nor in the switch file.

That macro selects `USE_CAL_IN_HOLD` on the autoneg instance
(`axi_chiplet_controller.sv:3244-3248`), and your own comment immediately above
it says:

> "M1 (2026-07-02): the L4 training-exit predicate retarget (cal_in_hold
> rendezvous + mask-aware lane checks + byte-3 capture) is **V2-ONLY**. On V1
> `cal_in_hold_w` is tied 0 below, **which would make the autonomous
> training-exit UNSATISFIABLE** … so V1 selects the exact pre-L4 predicate"

with the L4 fix itself described as removing "the old **circular deadlock**".
Several other autonomy fixes are gated the same way (M1 mask-awareness, R5
zombie-peer retry backoff, the `ST_FIN_RDV` entry arc).

On our pair the V1 predicate did clear — the poll exited at 3148.81 µs — so V1 is
not literally unsatisfiable here. But **the autonomous path is not validated in
the configuration the chiplets ship.** Please either state that V1 does not
support autonomy, or bring the V1 arm up to parity and cover it in CI.

---

## 🟡 P3 — smaller items found on the way

* **Autonomous bring-up takes ~3.8 ms vs ~90 µs manual — about 40×.** Not
  necessarily wrong for an I²C sideband (`ST_TRAIN_EXIT`'s 6-byte peer write
  alone spins `TXN_POLL ↔ TXN_CHECK` for ~651 µs), but nothing documents an
  expected budget, and every timeout in our benches — and any host bring-up
  script written against the manual timings — is sized far below it. **Please
  publish an expected worst-case autonomous bring-up time.**
* **`test_zeropoke_por` cannot be rebuilt from the eth chiplet's pinned
  TideLink (`3ed78fe`).**
  * `TIDELINK_PHY_V2=1`: `Error-[SFCOR] Source file "tidelink_sync_word.svh"
    cannot be opened`, included by
    `src/rtl/local_overrides/tidelink_lane_deskew_v2.sv:184`. That header does
    not exist anywhere in that checkout — `deps/tidelink-phy` is pinned at a
    commit that lacks it. (The compute chiplet's TideLink `3f3de09` has it at
    `deps/tidelink-phy/rtl/tidelink_sync_word.svh`.) This looks like a
    `deps/tidelink-phy` submodule pin that was not rolled forward with the
    override.
  * `TIDELINK_PHY_V2=0`: `Error-[NYI-NS] Replacing interface cell in logical
    library not yet supported`, on a duplicate `apb4_if` interface
    (`deps/axi-chiplet-controller/logical/interfaces/apb4_if.sv`) — VCS
    T-2022.06-SP2.
* **Bug N2 is fixed and working** — recording this so nobody re-opens it. At
  2485.4 µs, `swi_training_mode_r = 1` on **both** dies and
  `train_peer_lane_locked_w = 0xFF` on the master. `b2bfde5` is an ancestor of
  both `3ed78fe` and `3f3de09`.
* **The SoC-Labs L6 min-CR-emit gate is not implicated.**
  `socl_l6_cr_emit_count` saturates at 255 and `socl_l6_cr_emit_gate_ok = 1` on
  both dies well before the stall.

---

## What we need from you

1. **Which term holds `swi_swreset` (and hence `io_tx_reset` / `io_rx_reset` /
   `io_app_reset`) asserted after `ST_TRAIN_DONE`?** `u_autoneg.swreset_hold_r`
   is 0 and `train_ok_r` is 1, so the autoneg FSM is no longer driving it — yet
   the reset stays high for 4.8 ms. Is `fch_quiesced_r` (or another sticky)
   latching and never clearing on the V1 arm?
2. **Is the autonomous path expected to re-enable the link layer itself after its
   own training swreset,** the way the manual `LL_SWRESET_ON → LL_SWRESET_OFF →
   LL_ENABLE` triplet does? If yes, that injector is not firing here. If no, then
   autonomy needs an APB path on **every** die, which contradicts the zero-poke
   contract.
3. **Is V1 supported for autonomous bring-up at all?** (P2.) A straight "no,
   build V2" is a perfectly good answer — we would repoint both chiplets' flists.
4. **What is the expected worst-case autonomous bring-up time**, so we can size
   host timeouts and sim budgets?

## What we have already ruled out — please don't re-run these

| Ruled out | How |
|---|---|
| Heterogeneity / two different chiplet tops | Reproduced on two identical `nanosoc_eth_chiplet` dies |
| Cross-revision interop (`3ed78fe` vs `3f3de09`) | Reproduced with both dies on `3ed78fe` |
| The chiplet SoC integration | Manual posture reaches FCSM 4 through the identical stack (cal_done 89.8 µs) |
| `mask_hs_bypass_i` / `apb_debug_unlock_i` hard-coded to `1'b1` | Identical in the working and failing configurations |
| Bug N2 (peer `SWI_TRAINING_MODE` write not landing) | `swi_training_mode_r = 1` on both dies |
| A deadlock in `ST_TRAIN_POLL_PEER` or `ST_TRAIN_EXIT` | Both complete; `15 → 16` at 3800.27 µs, `train_ok = 1` |
| The SoC-Labs L6 min-CR-emit gate | Count saturated at 255, gate satisfied |
| RX framer mis-alignment / ECC rejection | `ecc_corrected = ecc_corrupted = in_error_state = 0`; the framer never starts because its input is the constant training word `0xED1412EB ×4` |
| A testbench budget problem | Watched 500,000 cycles (10 ms) past `cal_done` |
| `NEGO_CFG` as poke vs time-0 parameter | Identical results both ways |
| The LL bootstrap being required from cold POR | Manual posture with it removed still reaches FCSM 4 |

---

## Minimal reproduction

Needs VCS (or another simulator with Verilog library-map + config support) and
cocotb. No RTL is modified; every diagnostic is an additive cocotb `MODULE` that
logs and passes.

### Exact pins

| Repo | Commit |
|---|---|
| `NanoSoC-Hetrogeneous-Chiplet-Testing` | `9885fed` (branch `main`) |
| `deps/eth-chiplet` (`nanosoc-ethernet-chiplet`) | `384c1ac` |
| `deps/eth-chiplet/tidelink` | **`3ed78fe`** (`tl-tp-v0-baseline-36-g3ed78fe`) |
| `deps/eth-chiplet/tidechart` | `585e042` |
| `deps/compute-chiplet` (`NanoSoC-Compute-Chiplet`) | `c813519` |
| `deps/compute-chiplet/tidelink` | **`3f3de09`** (`v2026.07.16-chiplet-verified`) |
| `deps/compute-chiplet/tidechart` | `f7bc745` |

Toolchain: VCS **T-2022.06-SP2_Full64**, cocotb **2.0.1**, Python 3.10.

> `deps/eth-chiplet`'s working tree has `tidelink/deps/tidelink-phy` dirty against
> its recorded gitlink; that is what breaks the P3 V2 build.

### The homogeneous reproduction — no compute chiplet needed

This is the one to start from: it needs only `nanosoc-ethernet-chiplet` at
`384c1ac` with TideLink `3ed78fe`, and it shows the whole defect.

```sh
make deps-full                       # nested submodules; `make deps` is shallow

G2=deps/eth-chiplet/verif/g2_soc_pair
PP=$PWD/sim/g2_homog_probe

# GOOD: manual ROLE_CFG posture   -> FCSM 4 on both dies (cal_done 89.8 us)
make -C $G2 sim BUILD=/tmp/g2_b1 MODULE=test_g2_fcsm_probe \
     TESTCASE=test_b1_manual_with_ll PYTHONPATH=$PP

# BAD: autonomous NEGO_CFG=0x61   -> FCSM 1, then 0; never comes up
make -C $G2 sim BUILD=/tmp/g2_b3 MODULE=test_g2_fcsm_probe \
     TESTCASE=test_b3_autoneg_no_ll PYTHONPATH=$PP
```

Each `TESTCASE` must be its own sim invocation — a second bring-up inside one sim
does not re-converge `cal_done`.

### The full trace and the stuck-signal capture (heterogeneous pair)

```sh
rm -rf sim/het_pair/build              # stale-build trap: always clean first

# the decisive trace: 500,000 cycles past cal_done, every state transition
make -C sim/het_pair sim MODULE=test_f6_longwatch

# the stuck signals: io_tx/rx/app_reset after train_ok. No DUT writes at all.
make -C sim/het_pair sim MODULE=test_f6_late_ll \
     TESTCASE=test_f6_observe_after_train_ok

# the unrecoverable-by-software demonstration: LL bootstrap after train_ok hangs
make -C sim/het_pair sim MODULE=test_f6_late_ll \
     TESTCASE=test_f6_ll_enable_after_train_ok

# supporting evidence
make -C sim/het_pair sim MODULE=test_f6_diag       # FCSM state-1 gate terms
make -C sim/het_pair sim MODULE=test_f6_rxchain    # PHY -> llrx -> FCSM
make -C sim/het_pair sim MODULE=test_f6_trainrdv   # autoneg + swi_training_mode_r
make -C sim/het_pair sim MODULE=test_f6_fix        # train_auto_en = 0, two routes
make -C sim/het_pair sim MODULE=test_f6_i2cstall   # ST_TRAIN_EXIT's I2C txn
```

Signal paths used throughout, per die:

```
<die>.u_tidelink[_0].u_chiplet_controller.u_autoneg.state_r
<die>.u_tidelink[_0].u_chiplet_controller.u_autoneg.swreset_hold_r
<die>.u_tidelink[_0].u_chiplet_controller.train_ok_w
<die>.u_tidelink[_0].u_chiplet_controller.swi_training_mode_r
<die>.u_tidelink[_0].u_chiplet_controller.u_wlink.tl2wl.wlink_tidelinktl.state
<die>.u_tidelink[_0].u_chiplet_controller.u_wlink.tl2wl.wlink_tidelinktl.io_tx_reset
<die>.u_tidelink[_0].u_chiplet_controller.u_wlink.tl2wl.wlink_tidelinktl.io_app_enable
<die>.u_tidelink[_0].u_chiplet_controller.u_wlink.tl2wl.wlink_tidelinktl.en_ff2_tx_demet_io_out
<die>.u_tidelink[_0].u_chiplet_controller.u_wlink.llrx_io_link_data
```

---

*A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
license. Copyright 2026, SoC Labs (www.soclabs.org).*
