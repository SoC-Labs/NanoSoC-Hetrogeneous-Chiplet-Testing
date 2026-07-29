# Verification plan — NanoSoC heterogeneous chiplet pair

**Scope:** verify **all die-to-die communication and functionality** between the
**NanoSoC Ethernet Chiplet** and the **NanoSoC Compute Chiplet**, running as a
*heterogeneous* pair across two Xilinx KR260 boards joined at J21 by a ribbon
carrying the TideLink D2D interface.

**Companion documents:** [`ARCHITECTURE.md`](ARCHITECTURE.md) (the DUT, the bench,
every register address this plan cites) · [`TEST_MATRIX.md`](TEST_MATRIX.md)
(139 tests, ids, status) · [`REPO_LAYOUT.md`](REPO_LAYOUT.md) (level convention,
ownership) · [`../host/API_CONTRACT.md`](../host/API_CONTRACT.md) (the `hetsoc` API).

---

## 1. The honest starting position

Everything below is written against one uncomfortable fact:

> **The heterogeneous pair has never run. Not on silicon, not in simulation, not
> once.** What exists is a *homogeneous* proof — eth die_a ↔ eth die_b, the same
> design in two bitstreams — and a compute chiplet that has no FPGA port at all.

| | Status | Evidence |
|---|---|---|
| Eth ↔ eth link up on silicon (FCSM=4, `cal_done=1`, bilateral) | ✅ **M1 achieved 2026-07-27** | `SWI_LANE_STATUS` die_a `0x05890000`, die_b `0x27890000`; held on a read-only re-check |
| Eth ↔ eth cross-die SRAM transfer, both directions | ✅ passes | fwd `0xC0FFEE01` → `0x2D00_1000`; rev `0xB2A0FEED` |
| Eth ↔ eth IPC mailbox (`0x23`) + IRQ source latch | ✅ passes | 4 words + `MSG_VALID` + `IRQ_STATUS[0]` |
| Eth ↔ eth 2000-beat write soak | ✅ passes | 0 mismatches, `FCSM_min=4`, sticky faults `0x0` |
| Eth ↔ eth data plane **repeatably** | 🔴 **no** | intermittently wedges the PS AXI bus; wedged on ~2 of 3 repeats |
| Eth ↔ eth TideChart election | 🔴 **fails** | dual-root: both dies `is_root=1` |
| **Compute chiplet on any FPGA** | 🔴 **does not exist** | no `fpga/` dir, no bitstream, no KR260 target, no host tooling |
| **Het pair in simulation** | 🔴 **does not exist** | every committed pair TB instantiates the *same* top twice |

The job of this plan is therefore **not** "re-run what works". It is:

1. Build the two surfaces that do not exist (a het simulation, a compute FPGA port).
2. Port the proven homogeneous flows across a genuinely asymmetric address map.
3. Cover the large set of D2D functionality that has never been tested **in any
   form** — inbound confinement most conspicuously.
4. Do all of it on a data plane that is known to be able to hang the bench.

---

## 2. Goals and non-goals

**Goals**

- A layered, id-addressable test suite (`L0`–`L5`) covering every D2D feature
  enumerated in §5, executable by a tests team without re-deriving any address.
- A **design-agnostic** framework: the eth/compute asymmetries live in a target
  descriptor, never in a test.
- **Safety enforced in code**, not in prose. A hang becomes an exception; an
  out-of-window address is unreachable.
- Clear, checkable **exit criteria** (§6) so "does the het pair work?" has a
  yes/no answer at each stage.
- A **characterised** link — throughput, latency, credit occupancy, CRC-vs-time —
  not merely a working one.

**Non-goals (this repo)**

- Fixing the RTL. `G-WEDGE`, `G-TC`, `G-PTP` are upstream items; this repo
  *detects and gates on* them and feeds evidence back (the eth repo's
  `TIDELINK_SILICON_FEEDBACK.md` is the precedent).
- Building the compute FPGA port. That is `G-FPGA`, owned by the compute chiplet
  team; this repo states the requirement and is blocked on it.
- Firmware development. Tests requiring a released core are specified and marked
  `BLOCKED-G-FW`.
- ASIC signoff, lint, CDC. Those live in the source repos.

---

## 3. Device under test

Full detail in [`ARCHITECTURE.md`](ARCHITECTURE.md). The three-line version:

Two chiplets, one TideLink D2D link between them over a J21 ribbon. Each die's PS
reaches its SoC **only** through a narrow backdoor window. From the far die,
**exactly two** targets are reachable — a shared SRAM and an IPC mailbox — and the
address-translator CAM rewrites `addr[31:24]` to select which.

**The five asymmetries that make this heterogeneous rather than a re-run:**

| # | Asymmetry | Consequence for the tests |
|---|---|---|
| 1 | IPC mailbox at `0x23` (eth) vs **`0x2A`** (compute) | the CAM replace byte is **direction-dependent**; `L4-MBOX-01/02` are two different tests, not one run twice |
| 2 | D2D window `0x2E`/`0x2F` (32 MB) vs `0x40`/`0x60` (256 MB) | every `0x2E03_xxxx` literal is wrong on compute — and lands on a live SoC register (`mgr_remap_0`) |
| 3 | 1 TideLink vs 2; `PORT_COUNT` 1 vs 2 | per-die expectations, and a dangling compute link 1 |
| 4 | 2× Cortex-M0+ vs M0+ manager + Cortex-M4 | the `d2d_irq` → NVIC map differs entirely; ISR tests are per-die code |
| 5 | Ethernet MAC + live PHC (eth) vs neither (compute) | M2 and cross-die PTP are one-sided; compute's `.phc_seconds` is tied 0 |

**Roles.** Three independent "master" notions — TideLink role (strapped: eth =
master), TideChart root (elected: compute recommended), PTP grandmaster (eth, the
only die with a real time source). Conflating them is the most likely source of a
confused bring-up. See [`ARCHITECTURE.md` §1](ARCHITECTURE.md).

---

## 4. Verification strategy — the layers

Six layers, each defined by *what hardware it needs* and *what it can catch*. The
levels are the `REPO_LAYOUT.md` convention; the "cannot catch" column is the part
that matters, because every layer above exists to cover the one below's blind spot.

### L0 — offline host logic  ·  no board, no simulator  ·  CI: always  ·  23 tests

The address maths, the register decode, the target registry, the safety guards.

| Catches | Cannot catch |
|---|---|
| A wrong window base before it reaches `/dev/mem` | anything about the RTL |
| Eth literals leaking into a compute path | anything about the link |
| The `ROLE_CFG 0x2080` / `ROLE_STATUS 0x2084` trap | timing, calibration, drift |
| Missing/incorrect wedge guards (`guarded`, `require_link_up`) | whether the guard *works on hardware* |

**Why this layer is load-bearing:** on ZynqMP a wrong base is not a failed test,
it is an unrecoverable AXI hang. `L0-ADDR-03` is the test that makes the whole
bench safe, and it runs in CI on every commit with no hardware.

### L0-SIM — pre-silicon het-pair simulation  ·  no board  ·  CI: nightly  ·  18 tests

Two *different* chiplet tops back-to-back, D2D pads cross-wired, cocotb-driven.

| Catches | Cannot catch |
|---|---|
| Protocol/decode bugs before a board is touched | **reset ordering** — an idealised sim resolves demets cleanly and passes vacuously (`RISK-6`) |
| The compute decode ambiguity (`0x40` vs `0x41`) | **calibration time** — the sim *forces* `tb_early_exit_force_q`; silicon must actually wait |
| Inbound confinement, both directions | eye margin, drift, BER |
| The S→M direction, which no sim has ever run | PS-side wedge behaviour (no PS in the sim) |
| Whether a bit error is recoverable (`L0-SIM-18`) | anything about the ribbon or the pad ring |

**The honest caveat, quoted from the source:** the false-FULL wedge that reset
ordering produces is *"invisible to simulation … A green sim proves nothing about
reset ordering."* Treat `L0-SIM-17` as a smoke test, not a proof.

### L1 — one board, read-only probes  ·  wedge: none  ·  CI: nightly  ·  17 tests

Boot-ROM aliveness, config-plane reads, health counters. **Cannot wedge**: every
address is an RO/combinational register on the free-running system clock, and an
in-window miss is terminated by the CMSDK default slave with `SLVERR`.

| Catches | Cannot catch |
|---|---|
| A dead backdoor, a wrong bitstream, a failed AFI re-poke | anything requiring two boards |
| Wrong role straps before you try to bring the link up | anything requiring a write |
| The per-node FC baseline that L5 characterisation needs | the wedge (that lives on the data plane) |

### L2 — one board, config-plane writes  ·  wedge: low  ·  CI: nightly  ·  16 tests

Role lock, CAM programming, TideChart timeouts, perf enable.

| Catches | Cannot catch |
|---|---|
| A config plane that reads but does not write | anything crossing the link |
| The **warm-reset CAM trap** (`L2-CAM-04`) — `ROLE_CFG` survives `hresetn`, the CAM does not | far-die behaviour |
| The RO/RW `TC_DEVICE_CLASS` contradiction (`L2-TC-03`) | election convergence |

### L3 — two boards, link bring-up + control plane  ·  wedge: low  ·  CI: nightly  ·  24 tests

The link itself: role lock → calibration → data mode → FCSM=4. Plus TideChart
election/enumeration and the returner doorbell. **This is the CI gate.** It is
reliable and repeatable on the homogeneous pair, and it does not push a single
byte across the data plane.

| Catches | Cannot catch |
|---|---|
| Everything about bring-up, calibration, role resolution | the wedge — no data-plane traffic |
| A ribbon/pinout fault, distinguished from an RTL fault (`L3-LINK-12`) | payload integrity |
| Dual-root and every TideChart fabric behaviour | throughput, latency |
| The two board-killing operator errors, as guards (`L3-LINK-08/09`) | |

### L4 — two boards, cross-die data plane  ·  wedge: **HIGH**  ·  CI: **never**  ·  28 tests

Peer-aperture loads and stores: SRAM, mailbox, confinement negatives, interrupts,
DMA, PTP, debug, the ethernet relay.

| Catches | Cannot catch |
|---|---|
| Whether data actually crosses, in both directions, to both targets | sustained behaviour (that is L5) |
| **Inbound confinement** — the security boundary, untested anywhere today | the *rate* of the intermittent wedge |
| Cross-die interrupt sources, and (with firmware) delivery | |

🔴 **Attended only, behind an explicit flag, with recovery staged.** Every test in
this layer can hang both boards.

### L5 — soak, stress, characterisation  ·  wedge: **HIGH**  ·  CI: **never**  ·  13 tests

Turns "it worked once" into numbers: throughput, latency, credit occupancy,
CRC-vs-time, mean-transfers-to-wedge, deploy repeatability.

| Catches | Cannot catch |
|---|---|
| Intermittency — by definition the only layer that can | a root cause (it produces the evidence; the RTL team produces the fix) |
| Eye/thermal drift after the one-shot calibration | |
| Whether the interim health-poll mitigation actually helps (`L5-RECOV-03`) | |

### The layer contract

```
 L0 ─────► "the host cannot issue a dangerous address"
   L0-SIM ─► "the protocol and the decode are right"      ── before any board
     L1 ───► "this board is alive and I can see it"
       L2 ─► "this board accepts configuration"
         L3 ► "the two dies agree there is a link"         ── the CI gate
           L4 ► "data crosses, and only where it should"   ── attended
             L5 ► "and it keeps doing so, this fast"       ── attended
```

A failure at level *N* invalidates every result above it. The suite must therefore
**stop at the first gating failure** rather than reporting a cascade — the eth
repo's runner already does this (`link not up ⇒ SRAM/mailbox/soak skipped`) and it
is the right behaviour to inherit.

---

## 5. Coverage — what "all D2D communication and functionality" means

Enumerated so that "comprehensive" is checkable rather than asserted. Each row maps
to test ids in [`TEST_MATRIX.md`](TEST_MATRIX.md).

### 5.1 Physical and link layer

| # | Feature | What must be shown | Tests |
|---|---|---|---|
| 1 | **Ribbon / PHY integrity** | forwarded clock and 8 lanes each way survive the cable; a wiring fault is distinguishable from an RTL fault | `L3-LINK-12` |
| 2 | **Lane count / PHY version match** | both dies are 8-lane and both are V2 — a V1/V2 het pair is untested and must **fail**, not warn | `L3-LINK-10/11` |
| 3 | **Calibration (runtime winscan)** | `cal_done` asserts on both dies, against a *different design*; it only completes when the peer is also training | `L3-LINK-02`, `L0-SIM-02` |
| 4 | **One-shot calibration is a liability** | `calibrated_once_q` freezes the sampling point; `SWI_FORCE_RECAL` is the only re-cal path and the FSM never drives it | `L3-CAL-01/02` |
| 5 | **FCSM convergence** | both dies reach FCSM=4 (LINK_IDLE) bilaterally, with `cr_seen & crack_seen` | `L3-LINK-01/03` |
| 6 | **Correct gating signal** | `cal_done` is the gate; `lanes_locked==0xFF` is **not** (it self-deasserts after `S_DONE`); `link_active` is literally `role_locked` | `L3-LINK-06`, `L1-LINK-04` |
| 7 | **Link holds** | FCSM=4 survives a delayed read-only re-check, `lane_fault==0` | `L3-LINK-04` |

### 5.2 Role, identity and negotiation

| # | Feature | What must be shown | Tests |
|---|---|---|---|
| 8 | **Role strap → effective role** | eth resolves master (`effective_role=0`), compute slave (`=1`); the inversion is handled | `L1-LINK-02`, `L3-LINK-05` |
| 9 | **Role lock semantics** | W1S, POR-only clear; survives `hresetn`; a second write is ignored | `L2-ROLE-01/03` |
| 10 | **Role lock must not precede far-die presence** | the bench straps allow it; doing so is the false-FULL wedge class | `L3-LINK-09`, `L0-SIM-17` |
| 11 | **TideChart identity plane** | `DEVICE_CLASS`, `PORT_COUNT`, `RANDOM_ID`, `local_id` readable per die and per-die-correct | `L1-TC-01/02`, `L3-TC-01` |
| 12 | **Root election** | exactly one root; identical `TC_BEST_CLAIM`; a dual-root is diagnosable via `RANDOM_ID` | `L3-TC-02/03` |
| 13 | **Deterministic root** | the grandmaster can be *chosen*, not left to a race | `L2-TC-03`, `L3-TC-04` |
| 14 | **Enumeration + routing** | root ID 0, leaf ID 1, `total=2`, both ACTIVE; route table matches | `L3-TC-05/06` |
| 15 | **Fabric telemetry** | broadcast crosses; pre-enumeration broadcast is dropped | `L3-TC-07/08` |

### 5.3 Address translation and apertures

| # | Feature | What must be shown | Tests |
|---|---|---|---|
| 16 | **CAM rule encoding + programming** | `[0]=en [15:8]=match [23:16]=replace`; readback; arming order (CTRL last) | `L0-REGS-03`, `L2-CAM-01/02` |
| 17 | **Translation actually moves the address** | with the CAM off the far die sees the *untranslated* upper byte | `L4-SRAM-06`, `L0-SIM-07` |
| 18 | **Rule priority** | rule 0 wins over a conflicting rule 1 | `L2-CAM-06`, `L4-SRAM-07` |
| 19 | **CAM does not survive a warm reset** | `hresetn` clears it while `ROLE_CFG` survives — a live link with a dead translator | `L2-CAM-04` |
| 20 | **Per-die aperture bytes** | eth `0x2F`, compute `0x41`; and the `0x40`/`0x41` ambiguity is resolved | `L0-TGT-04`, `L0-SIM-15`, `L2-CAM-05` |
| 21 | **One aperture reaches one region** | switching between SRAM and mailbox requires a CAM reprogram, and that reprogram is safe | `L4-MBOX-06` |

### 5.4 Data plane

| # | Feature | What must be shown | Tests |
|---|---|---|---|
| 22 | **Inbound target 1 — shared SRAM** | payload lands in the far die's *real* `shared_sram_0`, verified by a far-die **local** read | `L4-SRAM-01/02` |
| 23 | **Inbound target 2 — IPC mailbox** | same, at `0x2A` (compute) and `0x23` (eth) | `L4-MBOX-01/02` |
| 24 | **Direction M→S** | eth → compute | `L4-SRAM-01`, `L4-MBOX-01` |
| 25 | **Direction S→M** | compute → eth — flagged by TideLink's own harness as the hard direction | `L4-SRAM-02`, `L4-MBOX-02`, `L0-SIM-04` |
| 26 | **Read round trip** | a peer *read* returns the payload over the link, both directions | `L4-SRAM-03/04` |
| 27 | **Multi-beat / burst** | 8-word sequences land in order (catches cross-beat off-by-one) | `L4-SRAM-05`, `L0-SIM-10` |
| 28 | **Back-to-back with no idle** | the HREADY combinational loop stays broken | `L0-SIM-14` |
| 29 | **DMA-driven bulk crossing** | DMA dst = peer aperture (never the TX aperture) | `L4-DMA-01/02` |

### 5.5 Confinement and security — **the largest untested area**

| # | Feature | What must be shown | Tests |
|---|---|---|---|
| 30 | **Only two inbound targets** | every excluded byte DECERRs cleanly and **does not wedge** | `L4-CONF-01`, `L0-SIM-08` |
| 31 | **Debug windows stay closed** | `0xA0`/`0xB0` are unreachable from the link until `G-SEC` lands | `L4-CONF-02` |
| 32 | **CPU code space stays closed** | no remote write into either core's bootrom/IMEM/DMEM | `L4-CONF-03` |
| 33 | **No remote reset / remap / re-flash** | `reset_ctrl_0`, remap and QSPI are unreachable | `L4-CONF-01` |
| 34 | **The asymmetry is enforced, not assumed** | a `0x23` mailbox write aimed at the compute die DECERRs | `L4-CONF-04` |
| 35 | **Compute's DAP has no off-die path** | compute deliberately withholds `d2d0/d2d1` from `dap_m` | `L4-CONF-05` |
| 36 | **TX-aperture wedge gate** | a link-down TX access takes a clean 2-cycle AHB ERROR; `hsel_tlapb` stays selectable so bring-up still works | `L0-SIM-13`, `L2-LINK-01` |

> **This block has never been tested — in simulation or on silicon.** The eth
> repo's only DECERR test is *outbound* (`tb_tx_gate.sv`); nothing exercises
> inbound confinement anywhere. It is the highest-value new coverage in this plan.

### 5.6 Interrupts and IPC semantics

| # | Feature | What must be shown | Tests |
|---|---|---|---|
| 37 | **Mailbox doorbell — the general-purpose cross-die IRQ** | a far-die `MSG_VALID` edge latches the near-die `IRQ_STATUS[0]`, firmware-free | `L4-MBOX-03` |
| 38 | **ACK handshake** | the return half of the protocol; slot reuse | `L4-MBOX-04` |
| 39 | **Both slots** | slot 0 and slot 1 target different cores' NVICs | `L4-MBOX-05` |
| 40 | **TideLink doorbell via the returner** | the *other* cross-die master, never validated on silicon; payload = free-credit count, so 0 credits ⇒ no IRQ | `L3-IRQ-01`, `L2-LINK-03` |
| 41 | **Packet-committed / PTP / TideChart sources** | `STATUS[4]`, `PTP_CTRL[2]`, `d2d_irq[14]` | `L4-IRQ-01/02/03` |
| 42 | **Actual ISR delivery** | the interrupt reaches a handler on a released core — with per-die NVIC bits | `L4-IRQ-04`, `L0-SIM-12` |

### 5.7 Flow control, error handling, recovery

| # | Feature | What must be shown | Tests |
|---|---|---|---|
| 43 | **Credit observability** | `CREDIT_COUNT`, `OBS_FC_CREDIT`, `PERF_CONG_STATE` readable and interpreted (`4096` = idle) | `L1-HEALTH-02/03`, `L5-PERF-03` |
| 44 | **The observability gap is documented** | those registers see the **sideband only**; the AXI data nodes need per-node reads | `L1-HEALTH-04` |
| 45 | **Sticky faults** | OVERRUN / UNDERRUN / MASTER_ERROR stay clear under load | `L1-HEALTH-01`, `L5-SOAK-01` |
| 46 | **Error injection → recovery** | a bit error / dropped ACK is recoverable, not terminal | `L0-SIM-18` |
| 47 | **Wedge detection + recovery** | a hang becomes an exception, then a per-target POR, then a retry | `L5-RECOV-02` |
| 48 | **Interim mitigation without a rebuild** | polling per-node CRC/Ack-Nack between transfers measurably reduces the wedge rate | `L5-RECOV-03` |
| 49 | **SW link reset** | `0x2E03_0208` bit[3] recovers a desynced link without a JTAG POR | `L2-LINK-02`, `L5-RECOV-04` |

### 5.8 Reset, power and lifecycle

| # | Feature | What must be shown | Tests |
|---|---|---|---|
| 50 | **Three reset regimes** | `poresetn` / `hresetn` / `role_locked` behave as documented and differ in what they clear | `L2-ROLE-03`, `L2-CAM-04` |
| 51 | **Asymmetric power-up order** | the link converges regardless of which die powers first | `L0-SIM-17`, `L3-LINK-09` |
| 52 | **Teardown and re-bring-up** | POR → deploy → bring up → transfer, ×N, deterministic | `L5-RECOV-01`, `L3-LINK-07` |
| 53 | **Never re-bring-up a live link** | encoded as a refusal, not a warning | `L3-LINK-08` |
| 54 | **Power-domain assumptions** | one domain on the bench; a link cannot power down unilaterally (`pad_clk_rx` belongs to the peer) | documented; no test |

### 5.9 Application-level function

| # | Feature | What must be shown | Tests |
|---|---|---|---|
| 55 | **PTP / PHC time sync over the D2D sideband** | the subordinate PHC's offset to the GM converges and holds — judged by offset, **not** `servo_locked` | `L4-PTP-01` |
| 56 | **Ethernet path (M2)** | LAN8720 link, MDIO bring-up, MAC out of loopback, a frame through | `L4-ETH-01` |
| 57 | **The end-to-end chiplet story** | a frame received by the eth die lands byte-exact on the compute die | `L4-ETH-02` |
| 58 | **Cross-die SWD debug (goal G3)** | one probe halts a far-die core | `L4-DBG-01` |

### 5.10 Performance and characterisation

| # | Feature | What must be shown | Tests |
|---|---|---|---|
| 59 | **Throughput** | sustained write bytes/s with variance | `L5-PERF-01` |
| 60 | **Latency** | peer read round trip, median and p99 | `L5-PERF-02` |
| 61 | **Sustained integrity** | N-beat soak, 0 mismatches, no sticky fault, FCSM held | `L5-SOAK-01..04` |
| 62 | **BER / eye drift over time** | CRC-vs-time correlated with the first wedge | `L5-CHAR-01` |
| 63 | **Deploy repeatability** | pass rate per stage over ≥10 reflash cycles | `L5-CHAR-02` |

---

## 6. Milestones and exit criteria

Each milestone is defined so that "done" is a command you can run and a value you
can read, not a judgement.

### M-H0 — the het pair is **simulable**

*Definition:* two different chiplet tops, back to back, exchanging data in cocotb.

| Exit criterion | Test |
|---|---|
| `tb_het_pair.sv` elaborates with 0 errors | `L0-SIM-01` |
| Both dies reach FCSM=4 with `cal_done=1` in sim | `L0-SIM-02` |
| eth→compute **and** compute→eth SRAM writes land in the far die's real SRAM | `L0-SIM-03`, `L0-SIM-04` |
| Both mailbox directions land, at `0x2A` and `0x23` respectively | `L0-SIM-05`, `L0-SIM-06` |
| CAM-off identity control passes both directions | `L0-SIM-07` |
| **Inbound confinement DECERRs, both directions** | `L0-SIM-08` |
| The compute peer-aperture byte is resolved to a single value with the real top in the path | `L0-SIM-15` |
| All L0 host-logic tests pass in CI | `L0-*` (23) |

*Blocked by:* `G-TB`, `G-ADDR`. *Needs no hardware.* **This is the only milestone
that is achievable today**, and it is the one that de-risks everything after it.

### M-H1 — the het link comes **up on silicon**

*Definition:* the two boards, different designs, agree there is a link.

| Exit criterion | Test |
|---|---|
| A compute-chiplet KR260 bitstream exists and loads (`fpga_manager=operating`) | `L1-PROBE-04` |
| The compute PS backdoor window is identified and recorded in the target descriptor | `L0-ADDR-02` |
| Compute boot-ROM probe passes | `L1-PROBE-02` |
| Role straps read correctly per die (eth master / compute slave) | `L1-LINK-02`, `L3-LINK-05` |
| **Both dies reach FCSM=4 with `cal_done=1`, concurrently, over the ribbon** | `L3-LINK-01/02` |
| `cr_seen & crack_seen` on both dies | `L3-LINK-03` |
| The link holds on a delayed read-only re-check, `lane_fault==0` | `L3-LINK-04` |
| ≥5 consecutive cold cycles converge | `L3-LINK-07` |
| Both board-killer guards are in place and tested | `L3-LINK-08/09` |
| Nothing wedges | all L1–L3 |

*Blocked by:* `G-FPGA`, `G-WIN`.
*Reference:* the homogeneous equivalent was achieved 2026-07-27 on the first
attempt — the runtime winscan calibrated against the peer first time.

### M-H2 — the het **data plane** works

*Definition:* data crosses, both directions, to both targets, and only where it
should.

| Exit criterion | Test |
|---|---|
| eth → compute SRAM, verified by a compute **local** read | `L4-SRAM-01` |
| compute → eth SRAM, verified by an eth **local** read | `L4-SRAM-02` |
| eth → compute mailbox at **`0x2A`** + `MSG_VALID` | `L4-MBOX-01` |
| compute → eth mailbox at **`0x23`** + `MSG_VALID` | `L4-MBOX-02` |
| The cross-die IRQ **source** latches on the receiving die | `L4-MBOX-03` |
| Read round trip returns the payload, both directions | `L4-SRAM-03/04` |
| CAM-off control confirms the translation is real | `L4-SRAM-06` |
| **Inbound confinement holds and does not wedge** | `L4-CONF-01..04` |
| A 1000-beat write soak passes with 0 mismatches and no sticky fault | `L5-SOAK-01` |

*Blocked by:* `G-FPGA`. *Degraded by:* `G-WEDGE` — until that is fixed, M-H2 is
achievable but **not repeatable**, and the suite must record a wedge rate rather
than claim a pass.

### M-H3 — **full functional** coverage

*Definition:* every D2D feature in §5 that does not need new RTL has been
exercised.

| Exit criterion | Test |
|---|---|
| TideChart: exactly one root, deterministically chosen | `L3-TC-02/04` |
| TideChart: enumeration, routes, telemetry, negatives | `L3-TC-05..08` |
| The returner doorbell path is validated on silicon (a first) | `L3-IRQ-01` |
| All cross-die interrupt **sources** observed | `L4-IRQ-01..03` |
| ISR **delivery** on a released core, per-die NVIC | `L4-IRQ-04` |
| DMA-driven bulk crossing lands byte-exact | `L4-DMA-01` |
| Bidirectional soak and mailbox soak pass | `L5-SOAK-03/04` |
| Teardown + re-bring-up ×N, deterministic | `L5-RECOV-01` |
| Ethernet M2 alive; a frame relayed to the compute die | `L4-ETH-01/02` |
| Cross-die PTP converges, **or** `G-PTP` is formally accepted as out of scope | `L4-PTP-01` |
| Cross-die debug halts a far core, **or** `G-SEC` is formally deferred | `L4-DBG-01` |

*Blocked by:* `G-FW`, `G-TC`, `G-PTP`, `G-SEC`.

### M-H4 — **characterised** and CI-green

*Definition:* the link has numbers attached and the suite runs unattended.

| Exit criterion | Test |
|---|---|
| `G-WEDGE` fixed: error injection shows recovery, not a wedge | `L0-SIM-18` |
| L4 promoted from attended-only to CI-eligible; a documented wedge rate of **0 over ≥50 consecutive data-plane runs** | `L5-CHAR-02` |
| Throughput and latency published with variance | `L5-PERF-01/02` |
| Credit occupancy under bidirectional load recorded | `L5-PERF-03` |
| CRC-vs-time over ≥1 h with no unexplained rise | `L5-CHAR-01` |
| Deploy repeatability ≥10 cycles, pass rate per stage | `L5-CHAR-02` |
| Nightly CI green for L0/L0-SIM/L1/L2/L3 for 14 consecutive nights | all CI-safe |

*Blocked by:* `G-WEDGE` — this milestone is **defined** by that fix.

---

## 7. Gap and risk register

### 7.1 Gaps — things that do not exist yet

| Gap | What is missing | Blocks | Owner | Notes |
|---|---|---|---|---|
| **`G-FPGA`** | **Any FPGA port of the compute chiplet.** No `fpga/` directory, no bitstream, no KR260 target, no Makefile target, no host tooling. The eth chiplet's `fpga/haps-sx` + `tidelink/fpga/targets/kr260-eth-chiplet/` is the obvious template. | all of M-H1..M-H4 | compute chiplet team | ⚠ Do not be misled by `nanosoc-compute-system/fpga/pynq_z2/` (SoC-level, PYNQ-Z2, no TideLink) or `nanosoc-compute-system/docs/KR260_IMPLEMENTATION_PLAN.md` (explicitly a planning doc, "no RTL / build-flow changes"). |
| **`G-WIN`** | The compute PS backdoor window base. Must come from the compute KR260 build's `.hwh` MEMRANGE. **Do not assume `0x4_0000_0000`.** | `L0-ADDR-02`, `L1-PROBE-02`, all het silicon | follows `G-FPGA` | |
| **`G-ADDR`** | Confirmation of the compute peer-aperture byte and CAM base with the **real** `nanosoc_compute_chiplet` top in the path (`0x40` vs `0x41`), plus the `haddr[24]` aliasing behaviour. | `L0-SIM-15`, `L2-CAM-05`, `L0-TGT-04` | Sim | Closeable **today** in simulation, with no hardware. Highest value-per-effort item in this plan. |
| **`G-TB`** | The heterogeneous cocotb testbench. Every committed pair TB instantiates the same top twice. | all L0-SIM | Sim | See [`ARCHITECTURE.md` §8](ARCHITECTURE.md) for the seven concrete changes. |
| **`G-WEDGE`** | Recovery restored on the five AXI data-plane FC nodes, with `SOCL_L7_MIN_CRACK_EMITS` scaled for the 40 ns silicon ratio. | M-H4; degrades all of L4/L5 | TideLink team | Already raised upstream. See `RISK-1`. |
| **`G-TC`** | TideChart: deterministic `DEVICE_CLASS`, a working `force_root`, `TC_CTRL[3]` reset that actually clears `election_done`, an election timeout wide enough for a real link. | `L3-TC-02/04..08` | TideChart team | ⚠ Partially contradictory across repos — see §8. |
| **`G-PTP`** | The compute PHC exports no live time (`.phc_seconds`/`.phc_nanoseconds` tied 0 on **both** compute links; short `phc_ahb` variant). | `L4-PTP-01` | compute chiplet team | Cross-die PTP is architecturally impossible in this pair until it changes. |
| **`G-FW`** | Firmware on a released core: mailbox `irq_enable` + NVIC ISER for ISR delivery; DMAC programming; ethernet MDIO/MAC/PicoTCP. Both dies boot-gate their cores in the PS flow. | `L4-IRQ-04`, `L4-DMA-01`, `L4-ETH-01/02` | firmware | |
| **`G-SEC`** | The `REMOTE_DBG_EN` gate + inbound firewall that makes opening the debug windows to the link safe. **Never land the target-list edit without it** — the RTL is symmetric, so opening it exposes both dies. | `L4-DBG-01` | SoC team | `L4-CONF-02` is the *negative* that must hold until then. |

### 7.2 Risk register

---

#### `RISK-1` — 🔴 The cross-die data plane intermittently wedges the board

**Severity: critical. Likelihood: observed, ~2 of 3 repeats.**

The link comes up reliably, the *first* cross-die transfer reliably passes, and a
*subsequent* access — read **or** write, either direction — intermittently hangs
the PS AXI bus with no software timeout. JTAG POR is the only recovery.

*Root cause* (RTL-evidenced, high confidence): the shipped build resolves the five
AXI data-plane FC nodes (AW/W/B/AR/R) to the **upstream, recovery-stripped** FCSM;
only the sideband node keeps the SoC-Labs recovery logic (`socl_reack`, the state-7
watchdog, the L9b/L9c pktnum-gap re-anchor). A single bit error or dropped ACK on
an AXI node therefore has **no recovery path**: the credit ring fills, the response
beat never returns, the PS SmartConnect saturates, the PL slave set wedges.

Two aggravating facts: `SUB_STALL_TIMEOUT` cannot see this failure (it only counts
while `hreadyout` is low; a lost response beat parks the bridge with `hreadyout`
high — hence a hard hang with no `SIGBUS`), and the `rd_pipe_r` read-completion
guard is absent from the shipped `tidelink_top`, so reads are at least as fragile
as writes.

| Mitigation | Test |
|---|---|
| L4/L5 are opt-in, attended-only, never in CI. The default suite is die-local. | policy |
| Every board operation is timeout-wrapped → `WedgeDetected`, never an infinite block | `L0-SAFE-01`, `L5-RECOV-02` |
| Recovery is scripted: per-target POR API on mapstone-dev, **one board at a time, ~8 s apart**, retry once on a transient "cable not found" | `L5-RECOV-02` |
| Verdicts use the far-die **local** read wherever possible, so the peer *read* is never on the critical path | `L4-SRAM-01/02`, `L4-MBOX-01/02` |
| Interim no-rebuild mitigation: poll per-node CRC / Ack-Nack **between** transfers; re-cal or FLUSH on a rising CRC or stuck FIFO rather than transacting into a wedge | `L5-RECOV-03` |
| Fix validated by error injection before L4 is promoted | `L0-SIM-18` |

**Detected by:** `L5-CHAR-02` (repeatability), `L5-RECOV-02` (the wedge itself),
`L1-HEALTH-04` (which node).

---

#### `RISK-2` — 🟠 Marginal eye + one-shot calibration

**Severity: high. Likelihood: confirmed as the intermittency trigger for `RISK-1`.**

The calibrator latches `calibrated_once_q` on the first `S_DONE` and permanently
gates off re-trigger; only `SWI_FORCE_RECAL` (W1P, POR-default 0) can re-cal, and
the FSM never drives it. The sampling point is frozen at bring-up, so jitter and
thermal drift make each subsequent transfer more likely to sample one bit wrong.

This is why the *first* transfer passes and later ones do not, and why a clean
2000-beat soak is a lucky BER window rather than proof of recovery.

**Additional het-specific concern:** the two dies are *different designs* with
different placement and routing. There is no reason to expect the same eye margin
as the homogeneous pair, in either direction. A het pair may be better or worse;
it will not be identical.

| Mitigation | Test |
|---|---|
| Characterise CRC and sync-detect vs time under load, and correlate with the first wedge | `L5-CHAR-01`, `L3-CAL-01` |
| Exercise forced re-cal as a recovery primitive | `L3-CAL-02` |
| Keep transfers short; re-cal between bursts | `L5-RECOV-03` |

---

#### `RISK-3` — 🔴 The compute chiplet has no FPGA port (`G-FPGA`)

**Severity: critical (schedule). Likelihood: certain — it is a present fact.**

Everything at L1 and above is blocked on a bitstream that does not exist and is not
on the compute chiplet's roadmap (`docs/STATUS.md` "suggested next steps" lists sim
work and pads; FPGA is not mentioned).

| Mitigation | Test |
|---|---|
| **Front-load M-H0.** The simulation surface needs no board and closes `G-ADDR` — the single most likely source of a wasted bench day. | all `L0-SIM-*` |
| Build the framework and the whole L0 layer against both descriptors **now**, so the day a compute bitstream exists the suite runs. | all `L0-*` |
| Keep the homogeneous eth pair as a live regression for the framework itself: `hetsoc` must drive eth↔eth correctly before it is trusted on eth↔compute. | see §9 |

**Detected by:** `L0-ADDR-02` and `L1-PROBE-02` fail as `BLOCKED-G-FPGA` rather
than silently skipping — the matrix makes the gap visible in every run.

---

#### `RISK-4` — 🟠 The compute address map is derived, not observed

**Severity: high. Likelihood: high.**

The compute peer aperture is `0x41` **by RTL derivation** but `0x40` **in every
passing compute sim**, because those testbenches deliberately bypass
`chiplet_d2d_decode`. The mailbox is at `0x2A`, not `0x23`. `0x2E00_0000` is a live
SoC register on compute. And the decoder examines only `haddr[24]` of a 256 MB
window, so 240 MB of it is alias.

A test written from the eth repo's constants will either DECERR (best case) or poke
a live compute SoC register (worst case).

| Mitigation | Test |
|---|---|
| Nothing but the target descriptor may hold an address; tests take addresses from it | `L0-ADDR-05`, `L0-TGT-03/04` |
| Resolve `0x40` vs `0x41` in simulation, with the **real** top instantiated, before any bench time | `L0-SIM-15` |
| A negative test proves the asymmetry rather than assuming it | `L4-CONF-04` |
| Treat `NanoSoC-Compute-Chiplet/docs/PEER_APERTURE_PROGRAMMING.md` as the **eth** document it is — its own header says so | doc hygiene |

---

#### `RISK-5` — 🟡 CAM reprogramming mid-flight

**Severity: medium. Likelihood: plausible secondary — every wedging run contained a
CAM reprogram and a direction change.**

The CAM is combinational and the address is latched once, so an in-flight
transaction is immune — but a replace-byte glitch coinciding with an address latch
could misroute. A het pair reprograms the CAM *more* often than a homogeneous one,
because the mailbox byte differs by direction.

| Mitigation | Test |
|---|---|
| Quiesce the link before every CAM rule write; add a settle delay | `L4-MBOX-06` |
| Arm `CTRL` last, always | `L2-CAM-02` |
| Re-program after any warm reset — the CAM does not survive `hresetn` | `L2-CAM-04` |

---

#### `RISK-6` — 🟠 Reset ordering / role-lock on a dead RX clock

**Severity: high. Likelihood: low on the bench, high in a real two-die system.**

`role_locked` gates the recovered-RX-clock reset **and** both sides of the a2l
ACK-pointer CDC. The bench straps (`apb_debug_unlock_i = mask_hs_bypass_i = 1`) let
a software `ROLE_CFG` W1S latch `role_locked` **while `pad_clk_rx` is dead** — which
releases those resets onto a clock with no edges. On the first real RX edges the
gray ACK-pointer synchroniser samples stale state, latches a lap-ahead value,
`a2l_full` sticks, and the link delivers ~6 words then stops. **Permanently.**

Compute compounds it: its reset ordering is explicitly *unanalysed*, and it has two
`user_ref_clk` and two `pad_clk_rx` async domains rather than one.

| Mitigation | Test |
|---|---|
| The framework refuses bring-up with no peer present | `L3-LINK-09` |
| Bring-up is always concurrent on both boards, on **fresh** dies | `L3-LINK-01` |
| Never re-bring-up a live link | `L3-LINK-08` |
| Exercise both power-up orders in sim — **while recording that a green sim proves nothing here** | `L0-SIM-17` |

---

#### `RISK-7` — 🟠 TideChart election is non-deterministic and currently fails

**Severity: medium (it blocks fabric features, not the data plane). Likelihood:
observed — dual-root on silicon.**

Both designs default to `DEVICE_CLASS = 0x0001`, so a het election ties on class
and falls to a free-running `random_id`. Add `force_root` being decoded but never
consumed, `TC_CTRL[3]` not clearing `election_done`, and a default election timeout
shorter than the D2D round trip, and the result is a coin flip you cannot override.

Orchestration makes it worse: the election window is at most ~1.3 ms, and two
independent SSH commands start seconds apart, so the windows never overlap and each
die self-elects on timeout.

| Mitigation | Test |
|---|---|
| Widen `TC_TIMEOUT` before every election | `L2-TC-01` |
| Establish whether `TC_DEVICE_CLASS` is RW on the shipped pin — if so, a deterministic root needs **no rebuild** | `L2-TC-03` |
| Then choose the root explicitly and prove it over ≥5 rounds | `L3-TC-04` |
| Diagnose a dual-root from `is_root` on both dies, **never** from `TC_ERROR[2]` (never set by HW) | `L3-TC-03` |
| TideChart stays **non-gating** in CI until `G-TC` closes | policy |

---

#### `RISK-8` — 🟡 First silicon, first het pair, two different designs

**Severity: medium. Likelihood: high.**

Neither chiplet has ever been paired with a *different* design. The eth chiplet has
residual TideLink RX setup violations (−2.9/−3.3 ns, 4 endpoints) — tolerable
because the interface is runtime-calibrated, but a second, differently-routed die
changes the margin on both sides. The link runs with **no header ECC** (a
deliberate, documented bring-up bypass), so header corruption is undetectable;
payload CRC still applies.

| Mitigation | Test |
|---|---|
| Treat the first het bring-up as a debug session, not a formality — `L3-LINK-12` separates "ribbon" from "SoC" before anyone blames the RTL | `L3-LINK-12` |
| Keep the homogeneous eth pair as the control: if the het pair fails, re-run eth↔eth to prove the bench | see §9 |
| Record `lane_fault`, `SYNC_DET` and per-node CRC on every bring-up, not just on failure | `L1-HEALTH-04/05` |

---

#### `RISK-9` — 🟡 Recovery infrastructure is itself fragile

**Severity: medium. Likelihood: observed.**

`fpgahub` per-board routes 404 from some client hosts (run them on
`mapstone-dev`); the group `board reset` breaks on the `_pl` topology member (use
the per-target API); back-to-back PORs produce transient "cable not found".

| Mitigation | Test |
|---|---|
| `hetsoc.fpgahub.reset()` encodes all three workarounds — per-target API, one board at a time, ~8 s gap, one retry | `L5-RECOV-02` |
| Recovery is tested deliberately, not discovered during an incident | `L5-RECOV-02` |

---

## 8. Contradictions and open questions in the source material

Recorded rather than resolved, because resolving them needs hardware or an RTL
owner. Each has a test that will settle it.

| # | Contradiction | Where | Settled by |
|---|---|---|---|
| 1 | **Compute peer aperture: `0x40` or `0x41`?** RTL derivation says `0x41` (decoder in path); every passing compute sim uses `0x40` (decoder bypassed by design). | `chiplet_d2d_decode.sv:113,138` vs `verif/g2_soc_peer_aperture/tb_soc_pair.sv`, `test_soc_peer_aperture.py:40,46,51` | `L0-SIM-15` |
| 2 | **`TC_DEVICE_CLASS`: RO or RW?** The eth repo says RO and concludes a rebuild is needed; the compute repo says it is now RW with a documented firmware contract. Different TideChart submodule pins. | eth `TIDECHART_TEST_PLAN.md:34,88-94` vs compute `docs/STATUS.md:73-89` | `L2-TC-03` |
| 3 | **Compute `PEER_APERTURE_PROGRAMMING.md` is the eth document**, carrying `0x2D`/`0x23`/`0x2E034000`. Its own header warns of this; a reader who skips the header is misled. | compute `docs/PEER_APERTURE_PROGRAMMING.md:3-12` vs `:40,155,179-200` | doc fix in the compute repo |
| 4 | **Compute `README.md` describes the chiplet top as "not written yet"** while the file is 58 KB with 81 ports and elaborates clean. `docs/G2_PAIR_SIM.md` and `PHYSICAL_HANDOFF.md` are similarly stale stubs. `docs/STATUS.md` is the current truth. | compute `README.md:7-13,122,130-139` | doc fix in the compute repo |
| 5 | **DMA part number.** The backlog calls it "DMA-250"; the SoC YAML says "DMA-230 APB configuration registers"; the firmware header says the block "exposes a PL230-interface-compatible register map" and the `DMAC_0_` prefix is retained deliberately. | eth `CROSS_DIE_TEST_BACKLOG.md` item 4 vs `nanosoc_multicore_soc.yaml:2164` vs `nanosoc_multicore_addrmap.h:229-232` | naming only — no functional impact; use the register map |
| 6 | **`verif/g2_peer_aperture/README.md` lists four test names that do not exist.** They were collapsed into one staged test. | eth `verif/g2_peer_aperture/README.md:5,27-32` vs `test_peer_aperture.py:296-308` | doc fix in the eth repo |
| 7 | **TideChart 3-die enumeration** is registered `expect_fail` with a README verdict table, but a later submodule bump added the multi-hop relay and the 4-die test passes. The flag and the table may be stale. | compute `verif/g3_tidechart_forward/` | re-run before trusting either |
| 8 | **Compute submodule pins disagree across three places** (`README.md` vs `STATUS.md` vs the actual gitlinks, which have been bumped again). | compute `README.md:85-93`, `STATUS.md:24,126-129`, git log | `git submodule status` before pinning a het environment |
| 9 | **Which TideLink pin does each bitstream carry?** `STATUS_REGISTERS.md` is explicit that the eth submodule (`3f3de09`) and the standalone `~/SoCLabs/tidelink` clone differ in line numbers *and semantics*. The het pair must pin one. | eth `STATUS_REGISTERS.md:8-13,269-271` | build manifest check at `L3-LINK-10` |
| 10 | **`PERF_CONG_STATE` address.** Historically `0x2E0320D8` (region off-by-one bug), now `0x2E0320F8` post-fix, and `PERF_ID` reads `0x5046_0100` only on the fixed pin. A script gating on `PERF_ID` fails silently on an old pin. | eth `STATUS_REGISTERS.md:211-215,223-230` | `L2-LINK-04` |

**Open questions with no answer in the source at all** (marked `[TBD]` in the
matrix): the compute PS backdoor window base; the compute boot-ROM expected words;
the `SWI_FORCE_RECAL` register offset (take it from the TideLink `REGISTER_MAP.md`);
whether a KR260 can pulse `hresetn` alone without a full POR (needed for
`L2-CAM-04`).

---

## 9. Regression policy

**The CI-safe suite is L0 + L0-SIM + L1 + L2 + L3.** None of it pushes a byte
across the data plane; all of it is repeatable. This is inherited directly from
the eth repo's hard-won conclusion: *"register-plane/link-up is a reliable
repeatable gate; the cross-die data plane is not yet CI-stable."*

| When | What runs | Gate |
|---|---|---|
| Every commit | L0 (23 tests) | blocking |
| Nightly | L0 + L0-SIM + L1 + L2 + L3 | blocking; TideChart rows non-gating until `G-TC` |
| On a design iteration | reflash both dies → nightly suite → **then** attended L4 | L4 attended-only |
| Attended bench session | L4 + selected L5, behind an explicit flag, recovery staged | records a wedge rate, not a pass |
| Milestone review | full matrix with status refreshed | §6 exit criteria |

**Three inherited operational rules, encoded as tests rather than prose:**

1. **Only ever bring up a link on FRESH dies** (right after a reflash).
   Re-running the bring-up on a live link desyncs it and hangs the sender.
   Without `--deploy`, the runner must *verify* (read FCSM=4), never re-bring-up.
   → `L3-LINK-08`.
2. **Stop at the first gating failure.** If the link is not up, skip everything
   downstream rather than reporting a cascade of failures with one cause.
3. **The framework proves itself on the homogeneous pair first.** `hetsoc` must
   drive eth↔eth to a full pass before it is pointed at eth↔compute — otherwise a
   het failure is ambiguous between "the pair is broken" and "the framework is
   broken". This is the control experiment, and it is available today.

---

## 10. Non-negotiables for anyone writing tests here

Lifted from [`../host/API_CONTRACT.md`](../host/API_CONTRACT.md) and the hazards
above. A test that violates one of these is rejected regardless of what it proves.

1. **No address may appear in a test.** Bases come from the target descriptor,
   offsets from `hetsoc.regs`. The eth/compute asymmetry is the reason this is a
   rule and not a preference.
2. **`Target.to_host()` fails loud on any out-of-window address.** There is no
   unchecked path to `/dev/mem`.
3. **Any peer-aperture access calls `require_link_up()` first.** A peer access on
   a down link hangs the PS bus.
4. **Every board operation is timeout-wrapped.** A hang raises `WedgeDetected`; it
   never blocks forever.
5. **L0 imports the whole package with no board, no `pynq`, no `/dev/mem`.**
   Hardware access stays behind lazy imports.
6. **Never point a bare-link script at a chiplet target** (`kr260_smoke.py`,
   `tl39.py`, `kr260_credit_tx.py`, `kr260_drain.py`, `kr260_onchip_*.py`). Their
   maps poke `0x8403_xxxx` / `0xA400_xxxx`, which are undecoded here.
   → `L1-PROBE-05`.
7. **Prefer a far-die local read as the verdict.** It proves the payload crossed
   without putting a peer *read* on the critical path.
8. **Judge the link by `cal_done` and FCSM.** Not `lanes_locked==0xFF` (it
   self-deasserts), not `link_active` (it is `role_locked`), not
   `PAIR_CREDIT_COUNTER` (reads 0 on a healthy link), not `servo_locked` (it
   reports the wrong servo).

---

## 11. References

**This repo:** [`ARCHITECTURE.md`](ARCHITECTURE.md) ·
[`TEST_MATRIX.md`](TEST_MATRIX.md) · [`REPO_LAYOUT.md`](REPO_LAYOUT.md) ·
[`../host/API_CONTRACT.md`](../host/API_CONTRACT.md) · `BENCH_RUNBOOK.md` ·
`SAFETY.md` · `BRINGUP_GAPS.md` · `SIM_PLAN.md` · `CI.md`

**Eth chiplet** (`/home/dam1n19/SoCLabs/nanosoc-ethernet-chiplet`):

| Path | Why it matters here |
|---|---|
| `docs/CROSS_DIE_TEST_BACKLOG.md` | the 9-item prioritised backlog this matrix grew from |
| `docs/CROSS_DIE_WEDGE_ROOTCAUSE.md` | `RISK-1` — the root cause, the diagnostics, the fix path |
| `docs/CROSS_DIE_INTERRUPTS.md` | the full `d2d_irq[15:0]` → NVIC map and every cross-die IRQ mechanism |
| `docs/CROSS_DIE_DEBUG_PLAN.md` | `L4-DBG-01`, `G-SEC`, the phased plan for goal G3 |
| `docs/KR260_BENCH_RUNBOOK.md` | the proven operator procedure and the M1 result |
| `docs/STATUS_REGISTERS.md` | the authoritative register table, and six traps |
| `docs/PEER_APERTURE_PROGRAMMING.md` | CAM layout, the one-region constraint, the invalid bring-up gates |
| `docs/TIDECHART_TEST_PLAN.md` | `L3-TC-*`, and the G1/G-FORCE/G-DUALROOT/G-TMO/G-VERIF gaps |
| `docs/TIDELINK_SILICON_FEEDBACK.md` | what was escalated upstream, and the precedent for doing so |
| `docs/RESET_ORDERING.md` | `RISK-6` — the three reset regimes and the false-FULL failure mode |
| `docs/POWER_DOMAINS.md` | why the link cannot power down unilaterally; CAM/ROLE_CFG retention |
| `docs/OVERNIGHT_WORKLOG.md` | the primary record of what actually ran and what failed |
| `docs/G2_SOC_PAIR_STATUS.md`, `docs/G2_TB_ARCHITECTURE.md` | the sim-pair precedent and the peer-write data-phase finding |
| `docs/CHIPLET_HOST_TOOLING_PLAN.md` | the target-descriptor design this framework implements |
| `tidelink/pynq_host/scripts/kr260_eth_{bringup,xfer,regress}.py`, `kr260_tidechart.py`, `eth_ss_probe.py` | the proven flows; every L1–L5 method is a generalisation of one of these |
| `verif/{g2_soc_pair,g2_peer_aperture,chiplet_d2d_decode}/` | the sim precedent, and the source of the bring-up recipe |
| `src/rtl/{nanosoc_eth_chiplet,chiplet_d2d_decode}.sv` | the decode and the IRQ vector assembly |
| `nanosoc-multicore-system/sys_desc/nanosoc_multicore_soc.yaml` | the address map and the inbound confinement list |

**Compute chiplet** (`/home/dam1n19/SoCLabs/NanoSoC-Compute-Chiplet`):

| Path | Why it matters here |
|---|---|
| `docs/STATUS.md` | **the current truth for this repo** — read it instead of `README.md` |
| `docs/PHYSICAL_HANDOFF.md` | the unanalysed reset ordering, the async clock count, no power domains |
| `nanosoc-compute-system/sys_desc/nanosoc_compute_soc.yaml` | mailbox at `0x2A`, the D2D windows, the inbound confinement list, the NVIC muxes |
| `src/rtl/{nanosoc_compute_chiplet,tidechart_shim}.sv` | two TideLinks, `NUM_PORTS=2`, the IRQ vector |
| `verif/{g2_soc_peer_aperture,g2_tidechart_election,g3_tidechart_forward}/` | the strongest compute-side proofs — and the decoder-bypass caveat |

**TideLink:** `tidelink/cocotb/VERIFICATION_PLAN.md` (the structural model for this
document), `tidelink/cocotb/tidelink_error_injection/` (the harness that gates
`G-WEDGE`), `REGISTER_MAP.md` (per-node FC registers, `SWI_FORCE_RECAL`).
