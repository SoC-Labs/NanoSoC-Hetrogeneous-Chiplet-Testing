# F6 — attribution: who owns the FCSM stall?

**Verdict: TideLink-side. High confidence.**

Two things turned out to be wrong in the original F6 write-up, and both matter:

1. **It is not a heterogeneity problem.** The failure reproduces exactly on a
   *homogeneous* pair — two identical `nanosoc_eth_chiplet` dies on the same
   TideLink commit — the moment the bring-up posture is switched from *manual
   `ROLE_CFG`* to *autonomous negotiation*. That posture is the only one
   available to the compute die (it exports no bus — SIM_PLAN F2), which is why
   the heterogeneous bench is the first thing to hit it.
2. **The FCSM does not stall at 1.** State 1 is a *transient* that the existing
   bench happens to sample. Autonomous training runs to completion at 3800 µs
   with `train_ok_w = 1`, and its final act — a link-layer software reset — drops
   **both** dies' FCSMs from 1 to **0**, where they stay indefinitely (observed
   for a further 8.7 ms). Nothing on the autonomous path re-enables the link
   layer afterwards.

Every previous measurement, including the whole `sim/het_pair` suite, gave up at
2.585 ms — *during* `ST_TRAIN_POLL_PEER`, 1.2 ms before training had even
finished.

Handover for the TideLink team: **`docs/TIDELINK_HANDOVER.md`**.

Measured on this host: **VCS T-2022.06-SP2_Full64**, cocotb 2.0.1, Python 3.10,
clean build directories (`rm -rf` before the first run of each configuration —
the lab's stale-build trap applies).

---

## 1. The 2×2, filled in with measured results

SIM_PLAN §8a framed the question as *heterogeneity*. The measurements say the
discriminating variable is the **bring-up posture**, so the table carries both
axes. Every row is a real run; logs and commands in §6.

| # | Configuration | Dies | TideLink rev | Posture | **FCSM, both dies** | role_locked | cal_done |
|---|---|---|---|---|---|---|---|
| **A** | raw `tidelink_top` pair | — | eth pin `3ed78fe` | zero-poke autoneg | **NOT MEASURED — would not build** (§5.1) | — | — |
| **B1** | homogeneous chiplet pair | 2 × `nanosoc_eth_chiplet` | `3ed78fe` both | manual `ROLE_CFG` + LL bootstrap | **4** ✅ | 80.6 µs | 89.8 µs |
| **B2** | homogeneous chiplet pair | 2 × `nanosoc_eth_chiplet` | `3ed78fe` both | manual `ROLE_CFG`, **no** LL bootstrap | **4** ✅ | 80.6 µs | 93.6 µs |
| **B3** | homogeneous chiplet pair | 2 × `nanosoc_eth_chiplet` | `3ed78fe` both | **autoneg** `NEGO_CFG=0x61`, no LL | **1** ❌ | 336.6 µs | 2567.1 µs |
| **B4** | homogeneous chiplet pair | 2 × `nanosoc_eth_chiplet` | `3ed78fe` both | autoneg + LL bootstrap at cal_done | **AHB write hangs** (F5) | 336.6 µs | 2567.1 µs |
| **C** | heterogeneous pair | eth + compute | `3ed78fe` / `3f3de09` | autoneg (forced — no bus on compute) | **1**, then **0** from 3800 µs ❌ | 256.4 µs | 2485.4 µs |

### What this settles

* **B1 = 4 ⇒ the chiplet integration is not the problem.** Two real
  `nanosoc_multicore_soc` dies, each behind `chiplet_d2d_decode` + `tidelink_top`,
  reach LINK_IDLE: cal_done at 89.8 µs, FCSM 4 confirmed at the first sample (192.4 µs).
* **B3 = 1, with zero heterogeneity.** Identical RTL, identical TideLink commit,
  identical testbench, identical calibrator sim-bypass. The **only** change from
  B1/B2 is arming `NEGO_CFG = 0x61` instead of writing `ROLE_CFG`.
  → **Cross-revision interop is eliminated.** The `3ed78fe`/`3f3de09` divergence
  (33 differing RTL files, SIM_PLAN §7) is real but is not what stops the link.
  B3 is a *stronger* control than SIM_PLAN's proposed "force both dies onto one
  revision" experiment, because it removes every heterogeneity rather than just
  the TideLink revision — so that experiment was not run.
* **B2 = 4 ⇒ the LL bootstrap is genuinely a no-op from cold POR.** SIM_PLAN §5's
  claim is now confirmed by measurement, not only by inspection of reset values.
  **But see §3: it is decidedly *not* a no-op after autonomous training.**
* **B4 reproduces F5 on identical dies.** F5 is a property of the posture, not of
  the heterogeneous pair — and §3 shows *why* it hangs: it is issued 1.3 ms too
  early, while the negotiation FSM still owns the register file.
* **C's signature is byte-for-byte B3's.** The heterogeneous bench is reporting a
  defect the homogeneous bench never exercises, because that bench can poke both
  dies and so never needs autonomy.

---

## 2. What is actually happening — the measured timeline

Heterogeneous pair, `sim/het_pair`, `MODULE=test_f6_longwatch`, watching every
autoneg and FCSM state change for 500,000 cycles after `cal_done`. Autoneg state
names are `tidelink_autoneg.sv:240-276`.

| t (µs) | Event |
|---:|---|
| 60.93 | die C autoneg `2 → 5` — **loses**, parks in `ST_NEGO_DONE` (its body is literally `// Terminal state — wait for POR`) |
| 255.99 | die E autoneg `4 → 9` — **wins**; `role_locked` on both dies at 256.4 µs |
| 534.83 / 1009.97 / 1661.43 | die E `9 → 10 → 8 → 11` — the I²C peer-mask exchange, then `ST_NEGO_DONE_PRE` |
| 1661.45 | die E `11 → 12` `ST_TRAIN_ENTER` — I²C-writes the peer's `SWI_TRAINING_MODE := 1` |
| 2312.91 | die E `12 → 13` `ST_TRAIN_RUN` |
| 2394.83 | die E `13 → 14` `ST_TRAIN_POLL_PEER` |
| **2485.4** | `cal_done` on both dies. **Both FCSMs at 1.** `swi_training_mode_r = 1` on **both** dies — the master's I²C write landed correctly |
| **2585.4** | ← **the existing suite gives up here.** All six failures are this sample |
| 3148.81 | die E `14 → 15` `ST_TRAIN_EXIT`; `train_peer_lane_locked_w = 0xFF` — the poll succeeded, all 8 peer lanes locked, no faults |
| 3148.8–3800.3 | `ST_TRAIN_EXIT` spins `TXN_POLL ↔ TXN_CHECK` for ~651 µs — the 6-byte I²C write of the peer's `SWI_TRAINING_MODE := 0`. Slow, but progressing |
| **3797.85** | **die C FCSM `1 → 0`** |
| **3800.27** | die E autoneg `15 → 16` `ST_TRAIN_DONE`; **`train_ok_w = 1`**, `train_fail_w = 0`, `train_in_progress_w = 0` |
| **3800.33** | **die E FCSM `1 → 0`** |
| 3800 → 12485 | **Both FCSMs remain at 0 for the whole remaining 8.7 ms.** die E autoneg 16, die C autoneg 5 |

**Autonomous negotiation and training therefore SUCCEED.** They are just slow
(3.8 ms vs ~90 µs for the manual posture), and they finish by switching the link
layer off and never switching it back on.

### 2.1 THE STUCK SIGNAL — the Wlink reset is never released after training

`WlinkGenericFCSM_6.v:1139-1157`:

```verilog
if (io_tx_reset)              state <= 3'h0;
else if (_fe_rx_ptr_in_T)     state <= 3'h0;
else if (_ack_seen_before_T)  begin        // _ack_seen_before_T = (state == 3'h0)
  if (en_ff2_tx_demet_io_out) state <= 3'h1;
end
```

State 0 exits only on `en_ff2_tx_demet_io_out` — `io_app_enable` 2-flop-synced
into `io_tx_clk` (`:1131`). Sampled at three points after `train_ok`
(`sim/het_pair/test_f6_late_ll.py`, `TESTCASE=test_f6_observe_after_train_ok`;
pure observation, no DUT writes at all):

| net (`…u_wlink.tl2wl.wlink_tidelinktl`) | 3800.6 µs | 4600.6 µs | 8600.6 µs |
|---|---|---|---|
| `state` | 0 / 0 | 0 / 0 | 0 / 0 |
| `io_app_enable` | **1 / 1** | **1 / 1** | **1 / 1** |
| `en_ff2_tx_demet_io_out` | **0 / 0** | **0 / 0** | **0 / 0** |
| **`io_tx_reset`** | **1 / 1** | **1 / 1** | **1 / 1** |
| **`io_rx_reset`** | **1 / 1** | **1 / 1** | **1 / 1** |
| **`io_app_reset`** | **1 / 1** | **1 / 1** | **1 / 1** |
| `reset` (APB domain) | 0 / 0 | 0 / 0 | 0 / 0 |
| `u_autoneg.swreset_hold_r` | 109 / 0 | **0 / 0** | **0 / 0** |
| `u_autoneg.train_ok_r` | 1 / 0 | 1 / 0 | 1 / 0 |
| `u_autoneg.state_r` | 16 / 5 | 16 / 5 | 16 / 5 |

*(values are `die E / die C`.)*

**This is the answer.** `io_app_enable` is high — the enable was never the
problem. What is held is the Wlink's **tx, rx and app resets, on both dies,
permanently**, while the APB-domain `reset` is released and the autoneg FSM has
finished (`ST_TRAIN_DONE`, `train_ok_r = 1`) with its own `swreset_hold_r`
counter already expired to **0**. The reset that `ST_TRAIN_EXIT`'s
`swreset_hold_nxt = T_SWRESET_HOLD` triggered **never de-asserts**, and it stays
asserted for the full 4.8 ms (240,000 cycles) watched.

It is not `wlink_por_reset` (`axi_chiplet_controller.sv:2832
wire wlink_por_reset = ~poresetn | ~role_locked;`) — `role_locked` is 1 on both
dies. It is the `swi_swreset` / `local_swreset_pulse_w` path
(`axi_chiplet_controller.sv:1339, 3340, 3398-3510`).

**And a host cannot recover it.** The Wlink register file lives in that held
domain, so the obvious repair — writing `WL_LINK_ENABLE_RESET (0x0208)` with the
`LL_SWRESET_ON / LL_SWRESET_OFF / LL_ENABLE` triplet after `train_ok` — does not
complete: measured, the **first** write hangs the AHB master for its full
50,000-cycle timeout (`test_f6_ll_enable_after_train_ok`, write issued at
3800.6 µs, timeout at 4800.65 µs). There is no software escape.

In the **manual** posture the autoneg FSM parks in `ST_BYPASS` (measured: B1/B2,
both dies `0 → 6` and no further transitions). No training swreset is ever
issued, the Wlink reset is never asserted, and the link is at FCSM 4 by the
first sample (192.4 µs; cal_done 89.8 µs) —
which is exactly why B2 passes with the LL bootstrap removed.

### 2.2 The state-1 snapshot, for completeness

Sampled at `cal_done + 40k` cycles (`test_f6_diag`), identically in C and B3:

| signal (`…u_wlink.tl2wl.wlink_tidelinktl`) | die E | die C | reading |
|---|---|---|---|
| `state` | 1 | 1 | mid-training |
| `io_app_enable`, `en_ff2_tx_demet_io_out` | 1 | 1 | still enabled at this point |
| `auto_tx_out_data_id` | 68 (`0x44` = CR) | 68 | emitting CR packets |
| `auto_tx_out_advance` | first 2486.03 µs | first 2485.45 µs | TX link layer accepting them |
| `socl_l6_cr_emit_count` | **255** (saturated) | **255** | ≥255 CRs emitted |
| `socl_l6_cr_emit_gate_ok` | 1 | 1 | **the SoC-Labs L6 gate is NOT the blocker** |
| `auto_rx_in_valid` | NEVER | NEVER | nothing ever received |
| `cr_pkt_seen_rx`, `crack_pkt_seen_rx` | NEVER | NEVER | consequence |

And the receive chain (`test_f6_rxchain`, 3,000 consecutive cycles, both dies):

* `phy_link_rx_rx_link_data` = `llrx_io_link_data` =
  **`0xED1412EB_ED1412EB_ED1412EB_ED1412EB`**, **exactly 1 distinct value**.
  That is the 8-lane GPIO **training pattern**: `WavD2DGpio.v:714-804` gives
  `WavD2DGpioTx` `TRAINING_PATTERN_HI = 8'h12` on even lanes and `8'hED` on odd,
  assembled as `{TRAINING_PATTERN_HI, io_training_pattern}` per lane
  (`WavD2DGpioTx.v:90-97,229-231`).
* `phy_link_tx_tx_link_data` **differs between the dies** (`0x…0011000f000f0014`
  vs `0x…0033003f003f000c`) — the Wlink TX is producing real packets; the PHY is
  simply still in training and not carrying them.
* `llrx.state/byte_count/word_count` = 0/0/0, `ecc_corrected = ecc_corrupted =
  in_error_state = 0` for all 3,000 cycles — the framer is not mis-aligning and
  not rejecting corrupt headers; it is being fed a constant.
* Pads are alive: 1,001 `pad_tx` and 1,999 `pad_clk_tx` transitions per 2,000
  `sys_fclk` cycles (eth), 996 / 1,999 (compute).

So the state-1 sample is entirely consistent and entirely benign: it is what
"training in progress" looks like.

---

## 3. Why F5 (the LL-bootstrap bus hang) is a timing artefact

The existing bench issues the LL bootstrap immediately after `cal_done`
(~2.5 ms) — i.e. squarely inside `ST_TRAIN_POLL_PEER`, while the negotiation FSM
owns the register file and I²C master, and **1.3 ms before training completes**.
That is when the write hangs (B4 reproduces it on identical dies).

Two consequences:

* F5's rule is too broad. Writes *before* the negotiation FSM claims the register
  file succeed — measured: a `NEGO_TRAIN_CFG (0x210C) = 0` write at 84.6 µs
  completed and read back `0x00000000` (§4). It is writes *during* negotiation
  that hang.
* There are in fact **two distinct hangs**, with different causes:
  * **during** negotiation (~2.5 ms, `io_tx_reset = 0`): the FSM owns the
    register file. This is what B4 and the original F5 observation hit.
  * **after** training (~3.8 ms onward, `io_tx_reset = 1`): the Wlink register
    file is inside the held reset domain (§2.1). This one is unrecoverable in
    software.
* The bootstrap is a no-op *from cold POR* (B2) and **cannot be used at all**
  after autonomous training, because the register file it targets is in reset.

---

## 4. Hypotheses tested and eliminated

| # | Hypothesis | Verdict | Evidence |
|---|---|---|---|
| 1 | The LL bootstrap is needed to drive the FCSM *from cold POR* | **Eliminated** | B2: manual posture with the bootstrap removed still reaches FCSM 4 |
| 2 | `NEGO_CFG` must be a parameter at time 0, not a post-reset poke | **Eliminated** (pre-existing; not retried) | SIM_PLAN §8a. Independently consistent: B3 arms it by *poke* and reproduces the *defparam* result exactly |
| 3 | **Prime suspect:** `tidelink_top` hard-codes `.apb_debug_unlock_i(1'b1)` / `.mask_hs_bypass_i(1'b1)`, so `mask_hs_auto_en` never does its part | **Eliminated** | The hard-coding is *identical* in both dies — eth override `src/rtl/local_overrides/tidelink_top.sv:2064-2065`, compute upstream `tidelink/src/rtl/tidelink_top.sv:2039-2040` — **and identical in B1/B2, which reach FCSM 4.** A constant with the same value in the working and the failing configuration cannot be the discriminator. Positively confirmed harmless: the peer-mask exchange (autoneg states 8/9/10) and the training rendezvous both complete. It remains a genuine wart — two chiplet boundary ports of the same name are silently ignored — but it is not this bug |
| 4 | Cross-revision interop between `3ed78fe` and `3f3de09` | **Eliminated** | B3: two dies on the *same* commit reproduce C exactly |
| 5 | The SoC-Labs L6 min-CR-emit gate is short | **Eliminated** | `socl_l6_cr_emit_count = 255` (saturated), `socl_l6_cr_emit_gate_ok = 1` on both dies |
| 6 | The RX framer mis-aligns / ECC rejects the headers | **Eliminated** | `ecc_corrected = ecc_corrupted = in_error_state = 0` for 3,000 cycles; the framer never starts because its input is a constant training pattern |
| 7 | The chiplet integration breaks the FCSM | **Eliminated** | B1/B2 reach FCSM 4 through the full chiplet stack |
| 8 | TideLink's **Bug N2** (peer's `SWI_TRAINING_MODE` write never lands) | **Eliminated** | `swi_training_mode_r = 1` on **both** dies at 2485.4 µs; `train_peer_lane_locked_w = 0xFF`. The `b2bfde5` Bug-N2 fix is an ancestor of both pins and is working |
| 9 | The autoneg FSM deadlocks in `ST_TRAIN_POLL_PEER` or `ST_TRAIN_EXIT` | **Eliminated** | It completes: `15 → 16` at 3800.27 µs with `train_ok_w = 1`. `ST_TRAIN_EXIT` spins `TXN_POLL ↔ TXN_CHECK` — a slow I²C write, not a freeze |
| 10 | Clearing `train_auto_en` is a usable workaround | **Eliminated** | See below |
| 11 | It is only a testbench budget problem | **Eliminated** | Watched 500,000 cycles (10 ms) past `cal_done`; both FCSMs sit at 0 from 3.8 ms to 12.5 ms |

### On hypothesis 10 — the documented escape hatch does not work here

TideLink's own RTL (`axi_chiplet_controller.sv:1278-1281`) records the on-silicon
escape hatch:

> "the proven manual recipe … disarms autonomy by writing **`NEGO_TRAIN_CFG
> 0x210C = 0` FIRST** (`td_v2_hwlib.sh` rcp :91), i.e. the real manual-vs-
> autonomous discriminator is `train_auto_en`"

Tested two independent ways on the heterogeneous pair
(`sim/het_pair/test_f6_fix.py`): as an APB write on the eth die at 84.6 µs
(`test_f6_disarm_via_apb`), and as a register deposit on both dies at reset,
mirroring the existing parameter `NEGO_TRAIN_CFG_RESET = 16'h0000`
(`test_f6_disarm_via_param`). **Both give the same result:**

* the master's autoneg FSM correctly declines the rendezvous — `… → 8 → 11 → 5`,
  skipping `ST_TRAIN_ENTER` entirely, exactly as the `train_auto_en` gate at
  `tidelink_autoneg.sv:1179` specifies; but
* **the master's `swi_calibration_done` then never asserts:**

  ```
  TimeoutError: cal_done never asserted on both dies within budget.
    eth R8_SWI_LANE_STATUS=0x40020000 (bit16=cal_done -> 0),
    compute calibration_done=1
  ```

With `train_auto_en = 0` the autonomous calibration FSM no longer owns the
per-lane slip (`REGISTER_MAP.md:302-306`) and, on the negotiation winner, nothing
else drives it to `S_DONE`.

---

## 5. The build-configuration finding — and what is still open

### 5.1 The chiplets build **V1**; TideLink's autonomy is proven on **V2**

TideLink's zero-poke proof (`cocotb/tidelink_top_pair/test_zeropoke_por.py`) is
documented to run with `TIDELINK_PHY_V2=1`, i.e. `flists/tidelink_fpga_v2.flist`.
**Both chiplets build the V1 flist** (`flists/tidelink_fpga.flist`, via
`flist/resolve_tidelink_flist.py`); verified — neither `sim/het_pair/build/eth_tidelink.f`
nor `cmp_tidelink.f` nor `switches.f` carries a `TIDELINK_PHY_V2` define.

That macro selects `USE_CAL_IN_HOLD` on the autoneg instance
(`axi_chiplet_controller.sv:3244-3248`), and TideLink's own comment immediately
above it says:

> "M1 (2026-07-02): the L4 training-exit predicate retarget (cal_in_hold
> rendezvous + mask-aware lane checks + byte-3 capture) is **V2-ONLY**. On V1
> `cal_in_hold_w` is tied 0 below, **which would make the autonomous
> training-exit UNSATISFIABLE** … so V1 selects the exact pre-L4 predicate"

and the L4 fix itself (`tidelink_autoneg.sv`, in `ST_TRAIN_POLL_PEER`) is
described as fixing "the old **circular deadlock**".

**This is the most likely explanation for A ≠ B3:** they are different builds of
the same RTL. On this pair the V1 predicate did in fact clear (the poll exited at
3148.81 µs), so V1 is not *unsatisfiable* here — but several autonomy fixes (L4,
M1, R5 zombie-peer retry, the `ST_FIN_RDV` entry arc) are compiled out of the
build the chiplets ship, and the autonomous path is not validated in that
configuration.

**Cell A itself could not be measured.** It would not build from the revision the
eth chiplet pins:

* `TIDELINK_PHY_V2=1` (the documented command):
  `Error-[SFCOR] Source file "tidelink_sync_word.svh" cannot be opened`,
  included by `src/rtl/local_overrides/tidelink_lane_deskew_v2.sv:184`. That
  header does not exist anywhere in `deps/eth-chiplet/tidelink` — its
  `deps/tidelink-phy` submodule is checked out at a commit that lacks it (the
  working tree shows it dirty against the recorded gitlink). The compute
  chiplet's copy has it at `tidelink/deps/tidelink-phy/rtl/tidelink_sync_word.svh`.
* `TIDELINK_PHY_V2=0` (the V1 flist — what the chiplets actually build):
  `Error-[NYI-NS] Replacing interface cell in logical library not yet supported`,
  on a duplicate `apb4_if` interface.

So "A = YES" is inherited from prior work, not confirmed here.

### 5.2 What this investigation did **not** determine

1. **What actually de-asserts the Wlink `swi_swreset`, and why it never fires.**
   §2.1 establishes *that* `io_tx_reset` / `io_rx_reset` / `io_app_reset` stay
   asserted after `train_ok`, and rules out `wlink_por_reset`. It does not
   identify which term in the `swi_swreset` / `local_swreset_pulse_w` /
   `fch_*` logic (`axi_chiplet_controller.sv:1339, 1352-1380, 2534,
   3340, 3398-3510`) is holding it. That is a TideLink-internal question and is
   question 1 of the handover.

2. **Whether the V1 build is the reason A ≠ B3.** Strongly suggested by
   TideLink's own comments (§5.1) but not demonstrated — cell A could not be
   built, so a V1-vs-V2 comparison on the raw pair was not possible.

3. **Whether the ~651 µs `ST_TRAIN_EXIT` I²C write and the ~3.8 ms total
   autonomous bring-up are expected.** They are ~40× the manual posture. Not
   necessarily wrong for an I²C sideband, but nothing states the expected budget,
   and every timeout in this repo (and, more worryingly, any host bring-up
   script) is sized well below it.

4. **No fix was proposed or attempted.** No RTL was modified. The only DUT writes
   made were configuration-register values the design already supports as POR
   parameters, in clearly-named diagnostic modules that assert nothing.

---

## 6. Reproducing

All diagnostics are additive: new cocotb `MODULE`s only. They modify no RTL, no
testbench and no existing test, and they assert nothing (they log and pass).

```sh
# C — the heterogeneous pair. Clean first; the stale-build trap is real.
rm -rf sim/het_pair/build
make -C sim/het_pair sim MODULE=test_f6_diag       # FCSM + its state-1 gate terms
make -C sim/het_pair sim MODULE=test_f6_rxchain    # PHY -> llrx -> rxrouter -> FCSM
make -C sim/het_pair sim MODULE=test_f6_trainrdv   # autoneg state_r + swi_training_mode_r
make -C sim/het_pair sim MODULE=test_f6_longwatch  # THE decisive one: 500k cycles
make -C sim/het_pair sim MODULE=test_f6_i2cstall   # ST_TRAIN_EXIT's I2C transaction
make -C sim/het_pair sim MODULE=test_f6_fix        # train_auto_en = 0, two routes
make -C sim/het_pair sim MODULE=test_f6_late_ll \
     TESTCASE=test_f6_ll_enable_after_train_ok     # the open experiment (§5.2)

# B1..B4 — the homogeneous control. Runs the ETH CHIPLET's own g2_soc_pair
# testbench read-only with our probe module, building into a scratch dir so
# nothing is written into the chiplet checkout.
G2=deps/eth-chiplet/verif/g2_soc_pair
PP=$PWD/sim/g2_homog_probe
for TC in test_b1_manual_with_ll test_b2_manual_no_ll \
          test_b3_autoneg_no_ll  test_b4_autoneg_with_ll; do
  make -C $G2 sim BUILD=/tmp/g2_$TC MODULE=test_g2_fcsm_probe \
       TESTCASE=$TC PYTHONPATH=$PP
done
```

Each `TESTCASE` must be its own sim invocation — a second bring-up inside one sim
does not re-converge `cal_done` (`test_g2_soc_pair.py:307`).

### Files added by this investigation

| File | What it is |
|---|---|
| `sim/het_pair/test_f6_diag.py` | The FCSM state-1 exit terms on both dies. `socl_l6_cr_emit_count` discriminates "cannot transmit" from "never receives" |
| `sim/het_pair/test_f6_rxchain.py` | pads → PHY → `llrx` → `rxrouter` → FCSM; counts distinct `llrx_io_link_data` values, so a static input is distinguishable from a mis-framing one |
| `sim/het_pair/test_f6_trainrdv.py` | `u_autoneg.state_r` + `swi_training_mode_r` on both dies — eliminated Bug N2 |
| `sim/het_pair/test_f6_longwatch.py` | **The decisive one.** 500,000 cycles past `cal_done`, logging every autoneg and FCSM transition on both dies |
| `sim/het_pair/test_f6_i2cstall.py` | `ST_TRAIN_EXIT`'s I²C transaction (`txn_step_r`, `busy_seen_r`, `axl_*`) — showed it spinning, not frozen |
| `sim/het_pair/test_f6_fix.py` | `train_auto_en = 0`, by APB write and by register deposit |
| `sim/het_pair/test_f6_late_ll.py` | Post-training link-layer re-enable — the open experiment |
| `sim/g2_homog_probe/test_g2_fcsm_probe.py` | The homogeneous control (B1–B4). Reads the FCSM, which `test_g2_soc_pair` never does |

### Exact pins

| Repo | Commit |
|---|---|
| `NanoSoC-Hetrogeneous-Chiplet-Testing` | `9885fed` (main) |
| `deps/eth-chiplet` | `384c1ac` |
| `deps/eth-chiplet/tidelink` | `3ed78fe` (`tl-tp-v0-baseline-36-g3ed78fe`) |
| `deps/eth-chiplet/tidechart` | `585e042` |
| `deps/compute-chiplet` | `c813519` |
| `deps/compute-chiplet/tidelink` | `3f3de09` (`v2026.07.16-chiplet-verified`) |
| `deps/compute-chiplet/tidechart` | `f7bc745` |

`deps/eth-chiplet`'s working tree has `tidelink/deps/tidelink-phy` dirty against
its recorded gitlink; that is what breaks the cell-A V2 build in §5.1.

---

## 7. Consequences for `docs/SIM_PLAN.md`

* **F6's description is wrong in its central claim.** The FCSM does not "stall at
  state 1 and never reach 4". It passes through 1 during training, is reset to 0
  by the training-exit swreset at 3.8 ms, and stays at 0. The suite's 2.585 ms
  sample is taken mid-training. §8a should be corrected.
* **F6 is not a heterogeneous-pair finding.** It reproduces on the homogeneous
  pair (B3). SIM_PLAN §1's "none of the six findings below is visible to either
  repo's own homogeneous pair" needs qualifying for F6 and F5: both are invisible
  to the homogeneous pair *as written*, because that bench pokes both dies and so
  never selects the autonomous posture — not because the pair is homogeneous.
* **F5's rule is too strong.** "Never replay the manual LL bootstrap on a die
  running autonomous negotiation" should be "never write the register file while
  the negotiation FSM owns it (through `train_ok`)". A `NEGO_TRAIN_CFG` write at
  84.6 µs succeeded; the same-file writes at 2.5 ms hang.
* **F3's recommended fix is necessary but not sufficient.** `NEGO_CFG_RESET =
  7'h61` does let both dies lock their roles from straps alone — confirmed again
  here — but it selects the posture that then leaves the link layer disabled. Any
  tape-out decision that relies on `7'h61` for a firmware-free die is blocked on
  the TideLink item in `docs/TIDELINK_HANDOVER.md`.
* **F2 is what makes this fatal rather than cosmetic.** If the compute chiplet
  exported an AHB slave, the manual posture would be available to it and the pair
  would come up today at FCSM 4, exactly as B1 does. F2 and F6 compound.

*A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
license. Copyright 2026, SoC Labs (www.soclabs.org).*
