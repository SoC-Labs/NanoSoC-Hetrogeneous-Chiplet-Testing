# eth → compute bring-up — the heterogeneous data-plane pair

**Status: SOFTWARE-READY, bench run pending (2026-07-31).** This is the
specialisation of [`BENCH_RUNBOOK.md`](BENCH_RUNBOOK.md) for the
**eth-chiplet → compute-chiplet** pair. It does not repeat the proven mechanics
(lease / deploy / bring-up / recovery) — read the BENCH_RUNBOOK for those and use
this doc for what is *different* about pairing with the compute die.

> 🔴 **Attended only.** The cross-die data plane (L4) can wedge the ZynqMP PS bus
> with no timeout; recovery is a **JTAG POR** on `mapstone-dev` only. Never run
> `make test-dataplane` unattended, in CI, or overnight. Open a POR terminal
> first (§Recovery). This is not a CI job.

---

## 0. What changed — why this pair is now runnable

Both compute KR260 bitstreams are **built** (2026-07-31, `write_bitstream`
Complete, 0 critical warnings, WNS +12.30 ns):

| target | role | balls | bitstream |
|---|---|---|---|
| `kr260-compute-chiplet` | **die_b** (receiver) | mirrored (TX=AC14, RX=AD15) | `NanoSoC-Compute-Chiplet/tidelink/imp/fpga/output/kr260-compute-chiplet/tidelink.{bit,hwh}` |
| `kr260-compute-chiplet-flip` | die_a | straight (TX=AD15, RX=AC14) | `.../output/kr260-compute-chiplet-flip/tidelink.{bit,hwh}` |

The host-side prep (this change set) resolved the compute target's PS window from
that `.hwh`, fixed `ps_reaches_d2d` propagation, and pointed `deploy_pair.sh` at
the correct die_b target.

### ⚠️ The compute chiplet is RECEIVE-ONLY over the PS backdoor
`ps_m` (the PS host initiator inside `nanosoc_compute_soc`) **deliberately
excludes `d2d0`/`d2d1`** (`nanosoc_compute_soc.yaml:1106-1123` — the H2 down-link
safety gate: no external host mastering off-die without a security review). So on
the compute die the PS backdoor **cannot originate a peer write and cannot program
its own CAM** — this is the *measured* `ps_reaches_d2d=False`.

Consequence for the pair:

* **eth is die_a and ORIGINATES.** Its PS programs the eth CAM (RULE_0) and drives
  its peer aperture `0x2F`; the transfer crosses the ribbon into the compute die.
* **compute is die_b and RECEIVES only.** The far write lands in compute
  `shared_sram_0` (`0x2D`) or `ipc_mailbox_0` (`0x2A`); compute's PS reads it back
  through its own window (`ps_m` *does* reach `shared_sram`). No CAM is programmed
  on the compute side.
* **compute → eth is NOT possible** without lifting the H2 gate (an RTL change +
  security review + rebuild). Do not attempt it.

### ⚠️ Naming gotcha — compute's `-flip` is INVERTED vs eth's
* eth: `kr260-eth-chiplet` = die_a (straight), `-flip` = die_b (mirrored)
* compute: `kr260-compute-chiplet` = **die_b (mirrored)**, `-flip` = die_a (straight)

So the compute die_b image is the **non-flip** `kr260-compute-chiplet`. Its
forwarded-clock balls (TX=AC14 / RX=AD15) are the exact complement of eth die_a,
so a plain **straight-through** J21 ribbon crosses TX↔RX correctly.
`deploy_pair.sh` now defaults `HETSOC_COMPUTE_TARGET_B=kr260-compute-chiplet`.

---

## 1. hetsoc.toml for this pair

Copy `hetsoc.toml.example` to `hetsoc.toml` and keep these sections. The compute
**target override** is the only non-obvious part — it de-provisionalises the
compute descriptor from the built `.hwh` (window base is board-wedging, so it must
cite its source; `hetsoc` refuses a TBD `source`):

```toml
[pair.default]
a = "eth"          # die_a, ORIGINATES
b = "compute"      # die_b, RECEIVES

[board.eth]
host    = "ubuntu@<ETH_DIE_A_IP>"      # e.g. kr260_01 = 10.22.24.159
fpgahub = "kr260_01"
target  = "kr260-eth-chiplet"
role    = "die_a"

[board.compute]
host    = "ubuntu@<COMPUTE_DIE_B_IP>"  # e.g. kr260_02 = 10.22.24.153
fpgahub = "kr260_02"
target  = "kr260-compute-chiplet"      # NON-flip = die_b (see naming gotcha)
role    = "die_b"

# De-provisionalise compute from its built .hwh. window_base/peer/inbound/
# ps_reaches_d2d=False are inherited from the registry; only window_size + a cited
# source are needed. (Already resolved in hetsoc.toml.example — keep it.)
[target.kr260-compute-chiplet]
window_size = 0x100000000
source = "kr260-compute-chiplet/tidelink.hwh MEMRANGE nanosoc_compute_chiplet_0 C_BASEADDR 0x400000000-0x4FFFFFFFF (built 2026-07-31, tidelink 74c6777)"
```

Put the board password in `$HETSOC_PASSWORD` / `$KR260_PASSWORD` or use ssh keys —
**do not commit it**. Verify the descriptor resolved:

```bash
source set_env.sh && make deps
python3 -c 'import sys; sys.path.insert(0,"host"); from hetsoc import config as C; \
  t=C.load().targets["kr260-compute-chiplet"]; \
  print("resolved=%s ps_reaches_d2d=%s window=0x%X" % (t.resolved, t.ps_reaches_d2d, t.window_base))'
# expect: resolved=True ps_reaches_d2d=False window=0x400000000
# (config.load() auto-discovers ./hetsoc.toml; pass a path to load() to override)
```

---

## 2. Physical setup — human, powered off

Identical to [`BOARD_WIRING.md`](BOARD_WIRING.md) — the compute die uses the SAME
J21 ribbon and the SAME rail-stripping. **Do not skip the power-rail strip** (phys
1 & 17 = +3V3, 2 & 4 = +5V): a full 40-way ribbon ties both boards' regulators
together and can damage both. Straight-through ribbon (BCM_n ↔ BCM_n), pin-1
confirmed on BOTH J21 ends, each board on its own barrel-jack PSU.

Compute exposes **two** TideLinks but the KR260 has one J21 — **link 0** (which
carries the TideChart) is the ribbon default; link 1 is tied off. The XDC already
routes link 0 to J21.

> The compute die_b J21 ball map is the mirror of eth die_b and has NOT been
> silicon-validated on this exact board yet — verify continuity + pin-1 with a
> meter before power if this is the first physical compute pairing.

---

## 3. Run sequence

Follows BENCH_RUNBOOK §3–§7. The eth→compute-specific points are called out.

```bash
# 0. Offline sanity (no boards)
make test-offline

# 1. Lease BOTH boards (pair-locked)
make lease                         # or: fpgahub lease acquire kr260_01 kr260_02

# --- human: cable the stripped straight ribbon, power die_a then die_b ---

# 2. Deploy — sequential, die_a (eth) then die_b (compute). NEVER concurrent.
make deploy-pair
#   die_a <- kr260-eth-chiplet      (eth, originator)
#   die_b <- kr260-compute-chiplet  (compute, receiver)   [now the default]

# 3. Prove each board alive + straps OPPOSITE (catches a swapped image in 1s)
make bench-status
#   eth die_a ROLE_STATUS -> master(0) ; compute die_b -> slave(1)
#   NOTE: on the compute die do NOT expect d2d/role regs via PS to be meaningful
#   beyond what the harness checks — ps_reaches_d2d=False is honoured, so the
#   harness reads compute liveness via shared_sram, not the CAM/role block.

# 4. Bring the link up on BOTH, concurrently (fresh dies only)
make bench-bringup                 # -> FCSM = 4 on both

# 5. Control plane (wedge-safe)
make test-pair                     # L3

# --- attended, POR terminal open on mapstone-dev (see Recovery) ---

# 6. Cross-die data plane — eth WRITES INTO compute
I_ACCEPT_WEDGE_RISK=1 make test-dataplane
#   test_l4_data_02: eth (die_a) peer-writes -> lands in compute (die_b)
#                    shared_sram_0 @0x2D; verdict = compute reads its OWN sram back.
#   test_l4_data_07/08 (mailbox): eth writes compute ipc_mailbox_0 @0x2A + IRQ.
#   The REVERSE tests (compute->eth, *_03) will NOT work here — compute cannot
#   originate. Select only the eth->compute cases if running individually.

# 7. Teardown
make release
```

### CAM words (programmed on the ETH die only)
`ChipletPair.map_peer_to(compute, which)` builds them heterogeneity-safely:
`match = eth.peer_aperture (0x2F)`, `replace = compute.inbound_byte(which)`:

| transfer | CAM RULE_0 word | note |
|---|---|---|
| eth → compute SRAM | `0x002D2F01` | replace 0x2F→**0x2D** |
| eth → compute mailbox | `0x002A2F01` | replace 0x2F→**0x2A** (compute mbox is 0x2A, not eth's 0x23 — 0x22/0x23 is the M4 SRAM bit-band alias) |

Only RULE_0 is programmed (SRAM *or* mailbox — reprogram the replace byte to
switch; there is no concurrent second rule in the host harness today). The CAM
resets on `hresetn`, so re-arm after any warm reset.

---

## 4. Recovery — a wedged board

Exactly [`BENCH_RUNBOOK.md` §10](BENCH_RUNBOOK.md). Before the first peer access,
open a terminal on `mapstone-dev` with the single-member POR ready:

```bash
ssh mapstone-dev "curl -s --unix-socket /run/fpgahub/fpgahub.sock \
  -X POST http://localhost/api/v1/targets/kr260_02/reset \
  -H 'Content-Type: application/json' -d '{\"method\":\"default\",\"confirm\":true}'"
# or:  make bench-recover BOARD=kr260_02      (omit BOARD= to POR both)
```

POR one board at a time (~8 s apart; a back-to-back second POR may report "cable
not found" — retry once). **A POR clears the PL → redeploy BOTH boards** before
continuing; never bring a half-loaded pair back up. **If the pair wedges twice on
the same test, STOP** — that is the H3 intermittent data-plane wedge (recovery-
stripped AXI FC nodes), an RTL/timing fix, not more POR cycles.

---

## 5. Caveats going in

* **Attended only** (repeated because it matters). L4 is not a CI/overnight job.
* **peer 0x41 is unverified in-path** on compute — but it is the *originator*
  aperture, and compute does not originate here, so it is moot for eth→compute.
* The `deploy_pair_role` command passes `SOC=kr260_eth` for both dies — that is
  only a bit2bin output label; the actual `.bit` is per-target and correct. It may
  mislabel the compute `.bin` filename; harmless.
* `HETSOC_COMPUTE_TARGET_B` now defaults correctly; if you override it, remember
  die_b = the **non-flip** `kr260-compute-chiplet`.
* This is the first physical compute pairing — meter the ribbon before power.

## References
* [`BENCH_RUNBOOK.md`](BENCH_RUNBOOK.md) — the proven eth↔eth procedure (mechanics)
* [`BOARD_WIRING.md`](BOARD_WIRING.md) — J21 ribbon, rail strip, pin-1
* [`BRINGUP_GAPS.md`](BRINGUP_GAPS.md) — G1/G2 (port), G4 (peer aperture), G7 (mailbox), H2/H3 (safety)
* `host/hetsoc/targets.py` — the compute descriptor (inbound bytes, ps_reaches_d2d)
* `host/hetsoc/pair.py` — `program_cam`, `map_peer_to`, `cross_die_write`
