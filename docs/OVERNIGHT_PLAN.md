# Autonomous overnight run — plan

**Goal.** Land a thorough, hardware-ready test set; absorb the updated compute
chiplet; and produce an honest analysis of what still fails and what work
remains.

---

## 0. Reality check — read this before approving

### The run CANNOT test on hardware. Three independent blockers:

| # | Blocker | Consequence |
|---|---|---|
| 1 | **No credential for `kr260_02`** — pings fine, `Permission denied (publickey,password)` from this host | every two-board level (L3+) is unrunnable |
| 2 | **L4/L5 wedge silicon by design** — recovery is a JTAG POR issued from `mapstone-dev` | attended-only; an unattended wedge leaves a dead board until morning |
| 3 | **No compute KR260 bitstream** (`G1` still open — there is still no `fpga/` in the compute repo) | the het pair cannot exist on boards at all |

So "a thorough test set we can run on hardware" means **authored, reviewed and
validated offline tonight, ready to execute the moment a credential and a
bitstream exist** — not executed tonight. Any plan promising overnight hardware
results is lying.

### What changed: the compute chiplet moved, and it matters

`/home/dam1n19/SoCLabs/NanoSoC-Compute-Chiplet` carries **three commits that are
not on `origin/main`** (origin is still `c813519`, 2026-07-24):

```
1a9ab1b  chiplet(d2d): G2 — re-export the PS host backdoor through the top
5ae10e3  rtl(d2d):     G4 — parameterise chiplet_d2d_decode on WINDOW_BASE + test in-path
23c9033  docs(align):  P0.2 — correct stale docs to match RTL reality (G17)
```

**`G2` is the blocker this repo called highest-leverage, and it is done.** The
compute top now exports `ps_ahb_s_*` — a full AHB slave, the `eth_ss_0`
analogue — and has grown 81 → 92 ports. That potentially unblocks three things
at once, which is what the night should be spent on.

And the routing is already confirmed in the system description — `ps_ahb_s`
*"becomes top-matrix initiator `ps_m` **reaching the whole compute map**"*
(`nanosoc_compute_soc.yaml:104`). Whole map includes the `d2d0` outbound window,
so Phase 2 below is likely to succeed **without firmware**, retiring the `G-FW`
conclusion in `SIM_PLAN.md §9b` barely a day after it was written.

> **Gate: BOTH levels are local and unpushed.**
> `NanoSoC-Compute-Chiplet` at **`1a9ab1b`** (2026-07-29) and its nested
> `nanosoc-compute-system` at **`b0b2218`** (`origin/main-5-gb0b2218`, i.e. 5
> commits past its own origin). The run pins both SHAs verbatim and records them
> in every commit and in the report. If either is rebased or force-pushed, every
> Phase 1–3 result becomes attributable to a tree that no longer exists — which
> is why "push these" is the first item in §3.

---

## 1. Phases

Each phase ends in a **gate**. A failed gate does not stop the run — it is
recorded and the run proceeds to the next independent phase. Nothing is
force-fixed to make a gate pass.

### Phase 0 — snapshot and bump  *(~30 min)*
1. Record current HEADs of every repo + submodule; tag the baseline.
2. Re-run the full green set as a **before** baseline: L0 (34), host unit (305),
   lint (5 gates), `make sim-het-manual` (7/7). Any pre-existing failure is
   captured now so it is never misattributed to the bump.
3. Bump `deps/compute-chiplet` to `1a9ab1b`; bump its nested
   `nanosoc-compute-system` if it moved. Re-run `make deps-full`.
4. **Gate A:** the het pair still elaborates. If G4's `WINDOW_BASE`
   parameterisation changed the decode, expect this to break — that is a
   *result*, not a failure of the run.

### Phase 1 — put the compute die on its real bus  *(~2 h)*
The manual posture currently **deposits** `role_cfg_reg`/`role_lock_reg`
hierarchically because the compute die had no bus. With `ps_ahb_s` it should no
longer need to.

1. Wire `ps_ahb_s` into `tb_het_pair.sv` as a second `AHBLiteMaster`.
2. Replace `_force_compute_role_slave()` with a **real APB write** through it.
3. **Gate B:** `make sim-het-manual` still 7/7, with the deposit gone.

**Why this is the highest-value hour of the night:** it converts the entire
`PROVEN-SIM-HET` result from "true with a testbench crutch" to "true through the
same path silicon will use". It also re-tests G2 end-to-end, which is exactly
what a fresh RTL change needs.

### Phase 2 — compute → eth, the untested direction  *(~2 h)*
`docs/SIM_PLAN.md §9b` concluded this was blocked on **firmware** because nothing
on the compute die could initiate. `ps_ahb_s` is an initiator. Re-test that
conclusion.

1. ~~Confirm `ps_ahb_s` routes to `d2d0`~~ — **already confirmed** above
   (`ps_m` reaches the whole compute map). Verify it in-path rather than on
   paper: a read of a known compute register over `ps_ahb_s` before trusting it
   as an initiator.
2. Program the compute CAM over `ps_ahb_s`; peer-write `0x41......` → eth `0x2D`.
3. Mirror for the mailbox → eth `0x23` (**note the asymmetry inverts in this
   direction**: eth's mailbox byte is `0x23`, not `0x2A`).
4. Inbound confinement in reverse: compute → eth `0x2A` must be **refused**.
5. **Gate C:** `L0-SIM-04`, `L0-SIM-06` pass, or a precise statement of what
   still blocks them.

TideLink flags slave→master as the harder direction, so treat a pass here with
more suspicion than usual — verify with the inbound-beat monitor on the eth die,
not just a read-back.

### Phase 3 — close the remaining sim rows  *(~2 h)*
In value order, from `docs/TEST_MATRIX.md`:
`L0-SIM-15` (settles `0x40` vs `0x41` **in-path** — G4 claims to have added
exactly this test, so cross-check rather than duplicate) · `L0-SIM-09`
(read round-trip both ways) · `L0-SIM-13` (TX-aperture wedge gate — the
no-backpressure path that hangs silicon) · `L0-SIM-14` (HREADY-loop guard, both
window shapes) · `L0-SIM-17` (asymmetric reset / far-die-dark) · `L0-SIM-18`
(error injection). `L0-SIM-11/12` (IRQ) and `L0-SIM-16` (TideChart election) only
if time remains — they are larger.

### Phase 4 — author the hardware set  *(~2 h)*
This is the "ready to run on hardware" deliverable. It is **authoring plus
offline validation**, never execution.

1. Extend `tests/` to cover the matrix rows that have no test, focusing on
   L1/L2/L3 (the levels that are CI-safe and will run first when a board frees).
2. Validate every one against `hetsoc`'s `MemoryTransport` mock — they must be
   collectable, importable, and correct-by-construction with **no board**.
3. Each must `skip` with an actionable reason when hardware is absent, never
   error.
4. **Gate D:** `pytest --collect-only` clean; `make test-offline` green; every
   new hardware test skips with a reason naming its blocker.

### Phase 5 — analysis  *(~1 h)*
Produce `docs/OVERNIGHT_REPORT.md`:
- every gate, pass/fail, with the measured evidence
- **the failing/blocked set**, each with: what fails, why, whose it is
  (SoC / TideLink / compute / this repo), and estimated effort
- coverage delta: matrix rows moved, and what percentage of each level is real
- a re-ordered remaining-work list, since G2 landing changes the critical path
- anything the run had to assume, and anything it could not verify

---

## 2. Guard rails (non-negotiable)

These exist because an autonomous run optimises for finishing, and the failure
mode is a green board that means nothing.

1. **Never weaken an assertion, stub a DUT, or mark a failing test xfail to
   reach a gate.** A recorded failure is a successful outcome.
2. **Mutation-test every new assertion.** An assertion that cannot fail is
   worthless — this repo has already caught one such (`_helpers` normalisation)
   and one live one (`FCSM_LINK_IDLE`).
3. **`rm -rf` the build dir before trusting any elaboration result.** The
   stale-build trap has already produced one false "PASS" on this project.
4. **No hardware.** No ssh to a board, no `fpgahub` lease/deploy/reset, no
   `/dev/mem`. Read-only `fpgahub status` is permitted.
5. **Never modify** `/research/AAA/**`, and do not edit the sibling chiplet
   repos — findings that need an RTL change get written up, not applied.
6. **Separate `BUILD=` per posture.** Shared dirs silently reuse the wrong
   `simv`.
7. **Commit per phase** with the measured evidence in the message; never one
   sweeping commit at 6am.
8. **Record every assumption.** If a value cannot be sourced, mark it TBD and
   name the file that must supply it — never guess an address.

---

## 3. What a human must do (blocking, in the morning)

| | Why |
|---|---|
| **Push the compute G2/G4 commits**, or confirm the SHA is stable | every Phase 1–3 result is pinned to an unpushed tree |
| **Provide a `kr260_02` credential** | unblocks all of L3+; nothing else does |
| **Decide on `G1`** (compute KR260 bitstream) | the only route to het silicon; still unstaffed |
| **Send `docs/TIDELINK_HANDOVER.md`** | F6 is still unfixed and is not ours |

---

## 4. Honest expected value

**Likely:** Phases 0–2 land, the compute die moves onto its real bus, and
compute→eth either works or is precisely characterised. That is a genuine
step-change in confidence and it retires the biggest caveat on the current
result.

**Possible:** the G4 `WINDOW_BASE` change breaks the het decode and Phase 0's
gate fails. That is *useful* — better found overnight than on a bench.

**Will not happen:** any statement about silicon. Every result will be
simulation, and the report must say so on every line.
