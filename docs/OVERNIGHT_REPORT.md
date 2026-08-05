# Overnight run — report

> ## ⚠️ SUPERSEDED IN PART — read this first  [2026-08-05]
>
> This is a **dated record of the 2026-07-29/30 run** and is kept as written.
> Four of its conclusions have since been overtaken by measurement. It is not
> rewritten, because a run report that quietly changes is worse than one with a
> correction notice — but do not action §6 from this file.
>
> | This report says | Now known |
> |---|---|
> | §6 rank 1: **"C1 — add `d2d0`/`d2d1` to `ps_m`, ~2 lines, cheapest unblock"** | **Wrong on both counts.** C1 is `H2`, a *deliberate* down-link safety property (`nanosoc_compute_soc.yaml:1106-1109`) — not a defect. And it is **not sufficient**: `F7` shows the compute die has no armed role-lock route at all, so the link cannot start even with `ps_m` routed. See `FPGA_TEST_PROGRAMME.md` §0.1 and `tests/test_l0_build.py::L0-BUILD-01`. |
> | §6 rank 6: **G1 compute KR260 bitstream, "3–6 wk, does not exist"** | **Closed.** Built 2026-07-31. Note its quoted WNS **excludes the D2D interface** — `pad_clk_tx_0_fwd` times zero endpoints — so that number says nothing about the link. |
> | §3: the return path needs firmware as a large new task | **Overtaken.** The compute firmware tree is **built** (`bootrom/spl/app/manager/manager_stage1.elf`, post-2026-08-01) and `manager_stage1/main.c` already references `ROLE_CFG`, `d2d0`, `d2d0_ahb_m`. H2 constrains `ps_m` only; `manager_m`/`compute_m`/`dma_250_0_m` all reach `d2d0`. |
> | Throughout: **"on silicon"** | **Nothing is fabricated.** In these repos "silicon" means the KR260 **FPGA** pair; eth's `ASIC/genus-innovus/outputs/` is empty. The usage was inherited from the repo docs and is misleading. |
>
> Also since: a **second hard stop** — both compute images pair one role's ball
> map with the other role's strap, so one pairing gives two masters and the other
> gives two drivers per ribbon conductor. **Do not deploy either compute image
> against eth die_a until rebuilt** (`L0-BUILD-04`/`L0-BUILD-05`).
>
> Current state lives in [`CHIPLET_ALIGNMENT_AUDIT.md`](CHIPLET_ALIGNMENT_AUDIT.md),
> [`FPGA_TEST_PROGRAMME.md`](FPGA_TEST_PROGRAMME.md) and
> [`SYSTEM_APPLICATION_PROPOSAL.md`](SYSTEM_APPLICATION_PROPOSAL.md).

**Run:** 2026-07-29 23:06 → 2026-07-30 08:2x. Plan: [`OVERNIGHT_PLAN.md`](OVERNIGHT_PLAN.md).
**Result:** all five phases executed. One gate partial, one blocked — both with a
named cause, neither force-fixed.

Every number below was measured during the run. **Nothing here is a silicon
result**; the run had no hardware access by design.

---

## 1. Gates

| Phase | Gate | Verdict | Evidence |
|---|---|---|---|
| 0 bump to G2 | A: het pair still elaborates | **PASS** | `simv_het`, 302 modules; suite 7/7 unchanged |
| 1 compute on its real bus | B: deposit removed | **PARTIAL** | bus works; blocked by **C1** below |
| 2 compute → eth | C: `L0-SIM-04/06` pass | **BLOCKED** | cause refined — C1 is now the cheapest unblock |
| 3 sim coverage | per-row | **PASS** | `L0-SIM-13`, `L0-SIM-17` added, both mutation-tested |
| 4 hardware set | D: offline-validated | **PASS** | L0 34 → 36; host unit 305; lint 5/5 |
| 5 analysis | — | this document | |

### Suite state at the end

| | before | after |
|---|---|---|
| `make sim-het-manual` | 7/7 | **10/10** |
| `make test-offline` (L0) | 34 | **36** |
| `host/tests_unit` | 305 | 305 |
| `make lint` | 5 gates clean | 5 gates clean |

Pins: repo `65a5b69` · eth-chiplet `384c1ac` · compute-chiplet **`1a9ab1b`** ·
nanosoc-compute-system **`b0b2218`**. **The last two are still unpushed.**

---

## 2. The headline: G2 landed, and it is one routing change short of useful

The compute chiplet gained `ps_ahb_s` — the PS host backdoor, the `eth_ss_0`
analogue this repo had called the single highest-leverage missing piece. It
works: a write + read-back to compute `shared_sram_0` @`0x2D002000` returns
`0xa5a5beef`.

**But it cannot reach the D2D window.** `ps_m`'s target list
(`nanosoc_compute_soc.yaml:1110`) omits `d2d0`/`d2d1`; `manager_m`, `compute_m`
and `dma_250_0_m` all have them. So the backdoor reaches the entire internal map
and none of the TideLink APB, CAM, or peer aperture:

| read over `ps_ahb_s` | result |
|---|---|
| `0x2D002000` shared SRAM (write + read-back) | `0xa5a5beef` ✅ |
| `0x40032080` ROLE_CFG | `0x00000000` |
| `0x40032084` ROLE_STATUS (after writing `0x03`) | `0x00000000` |
| `0x40032108` SWI_LANE_STATUS | `0x00000000` |
| `c_role_locked_o_0` | `0` |

**Why this is worse than it looks.** The failure is silent: the access succeeds
at the AHB level and returns zeros, and zeros decode as `fcsm=0 / cal_done=0` —
indistinguishable from a down link. A bring-up script reports "link failed" and
an operator goes and checks a ribbon that is fine. That is why the run put a
guard in the framework rather than only a note in a doc.

---

## 3. Findings, by owner

### Compute repo — 3 findings, none patched (guard rail 5)

| # | Finding | Cost to fix | Blocks |
|---|---|---|---|
| **C1** | `ps_m` omits `d2d0`/`d2d1` | ~2 lines of yaml | **all host-side compute bring-up**; cheapest unblock for compute→eth |
| **C2** | G2/G4 test hard-codes `CS ?= /home/dam1n19/SoCLabs/temp/compute-system` — an absolute scratch path outside the repo, at pre-G2 `42d9fdf` with zero `ps_ahb_s` | 1 line | the test cannot elaborate against the post-G2 top (8× `Undefined port`); G4's "test in-path" claim was validated against a tree that is **not** the one that ships; unreproducible off this machine |
| **C3** | SoC regen gated on presence, not freshness | small | same class as C2; fixed in this repo, still present there |

**C2 detail worth keeping:** the test *itself* is sound. Pointed at the pinned
submodule it passes — `soc_peer_store_crosses_link_to_far_sram PASS`, 1.80 ms
sim, 70 s wall. Only its default path is wrong.

### This repo — 2 bugs found and fixed

- **Stale-SoC regeneration.** Regen was gated on `[ ! -f compute_soc.flist ]`, so
  a `sys_desc` change never triggered a rebuild. After the bump the chiplet top
  instantiated `ps_ahb_s_*` against a cached generated SoC that did not declare
  them → 10× `Error-[UPIMI-E] Undefined port`, pointing at the RTL rather than
  the artifact. Anyone bumping the pin would have concluded G2 was broken. Now
  compares mtimes and `rm -rf`s first; verified both ways.
- **Dispatcher exported empty paths.** `sim/Makefile` did a bare
  `export ETH_CHIPLET_HOME` on an undefined variable, which exports it *empty* —
  and empty-but-exported still counts as set, beating `het_pair`'s `?=` default.
  Broke `make sim`, `sim-het-pair`, `sim-het-manual` while
  `make -C sim/het_pair` worked. Pre-existing.

### Settled from RTL

`0x40` vs `0x41` is **closed**. G4 parameterised `chiplet_d2d_decode` on
`WINDOW_BASE`; compute link 0 is `.WINDOW_BASE(32'h4000_0000)`
(`nanosoc_compute_chiplet.sv:525`) giving config `0x40xx`, **peer `0x41xx`**,
tlapb `0x4003_0000`. G4 also fixed the bypass that hid the defect — the compute
TB now takes `hsel` from the real decoder.

---

## 4. New coverage

| ID | What it proves | Note |
|---|---|---|
| `L0-SIM-13` | link-down TX-aperture write returns `AHBResp.ERROR`, bus stays usable | was eth-window-only; now in-path on the het pair |
| `L0-SIM-17` | far die dark → near die fully usable, no false `link_active`, TX gate still holds | the routine bench case: one board POR'd while the other is live |
| `L0-ADDR-19` | D2D addresses refused on a die whose PS port cannot reach them | encodes C1 |
| `L0-ADDR-20` | the same guard does **not** fire on the eth die | protects the only working path |

All four mutation-tested. One mutation was **inconclusive and is recorded as
such**: retargeting `L0-SIM-13` at `SWI_LANE_STATUS` still errored, but only
because that register is read-only. Re-mutating at `ROLE_CFG` (writable,
reachable pre-link) returned `OKAY` and killed the mutant properly.

---

## 5. Matrix state

139 rows. Status distribution after the run:

| Status | Rows |
|---|---:|
| PLANNED | 59 |
| BLOCKED-G-FPGA | 15 |
| PROVEN-HOM (homogeneous silicon) | 13 |
| **PROVEN-SIM-HET** | **9** |
| BLOCKED-G-TC | 7 |
| BLOCKED-G-FW | 5 |
| BLOCKED-G-TB | 4 |
| BLOCKED-G-ADDR | 3 |
| FLAKY-HOM / G-WIN / G-WEDGE | 1 each |

The honest read: **9 of 139 rows (~6%) are proven on the heterogeneous pair in
simulation, 0% on heterogeneous silicon**, and the largest single blocked group
(15 rows) is still waiting on a compute KR260 bitstream that does not exist.

> **A self-inflicted error, caught and fixed during this phase.** A `sed` in
> Phase 3 matched a status string that appeared on **two** rows, so `L0-SIM-14`
> (HREADY-loop guard) was marked `PROVEN-SIM-HET` although no test for it was
> ever written. Reverted to `PROVEN-SIM (eth window only) — NOT yet run in-path`.
> The count above is the corrected one. Worth recording because a matrix that
> over-claims is worse than one with gaps: the gaps are visible.

---

## 6. Remaining work, re-ordered

G2 landing changes the order. Previously "staff the compute FPGA port" was the
only lever; now there is a much cheaper one in front of it.

| # | Work | Owner | Effort | Unblocks |
|---|---|---|---|---|
| 1 | **C1** — add `d2d0`/`d2d1` to `ps_m` | compute | ~2 lines | host-side compute bring-up; `L0-SIM-04/06`; removes the last TB crutch from the het result |
| 2 | **Push** `1a9ab1b` + `b0b2218` | compute | minutes | every result in this report is pinned to an unpushed tree |
| 3 | **C2** — default `CS` to the submodule | compute | 1 line | makes the G2/G4 regression test real |
| 4 | **Send the TideLink handover** | you | minutes | F6 is unfixed and is not ours |
| 5 | `kr260_02` credential | you | minutes | every L3+ hardware level |
| 6 | **G1** — compute KR260 bitstream | compute | 3–6 wk | 15 matrix rows; the only route to het silicon |
| 7 | F6 fix (or build V2) | TideLink | unknown | autonomous bring-up; removes the manual-posture caveat |

Items 1–3 are together maybe an hour of someone's time in the compute repo and
would retire more blocked rows than anything else available.

---

## 7. What this run could not do

- **No hardware, at all.** No credential for `kr260_02`, L4/L5 wedge silicon and
  need an attended JTAG POR, and no compute bitstream. Stated up front in the
  plan; unchanged.
- **Cell A of the F6 2×2** (raw `tidelink_top` pair) still unmeasured — the
  upstream `test_zeropoke_por` will not build from the eth chiplet's pinned
  TideLink (V2 flist misses `tidelink_sync_word.svh`; V1 hits VCS `NYI-NS` on
  duplicate `apb4_if`). Not needed for the F6 verdict.
- **`L0-SIM-09/11/12/14/15/16/18`** not attempted — Phase 3 was cut short by the
  C1 investigation, which was the better use of the time.
- **The compute descriptor's prose is partly stale.** It still says the die has
  "no `eth_ss_0` analogue". G2 gave it one; what it lacks is the routing.
  Corrected in the `ps_reaches_d2d` docstring, not yet in the older block above
  it.

---

## 8. Assumptions

1. The compute commits stay at `1a9ab1b` / `b0b2218`. If rebased, every Phase 1–4
   result references a tree that no longer exists.
2. The manual posture's compute role-bit deposit is still a testbench crutch —
   the run tried to remove it and could not, because of C1. `PROVEN-SIM-HET`
   therefore still means "data plane proven, autonomous bring-up not".
3. `ps_reaches_d2d=False` is the correct encoding of C1 *today*. When C1 is
   fixed the flag flips and `L0-ADDR-19` must be updated with the commit that
   fixed it — the test says so rather than allowing a silent flip.
