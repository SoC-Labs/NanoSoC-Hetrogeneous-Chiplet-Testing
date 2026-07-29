# Bench runbook — the two-board heterogeneous chiplet pair

Operator steps to get a NanoSoC chiplet pair running across two Xilinx KR260s
joined by a J21 ribbon carrying the TideLink D2D interface.

> ### 🔴 Read [`SAFETY.md`](SAFETY.md) first. Not a formality.
> Any PS read of a PL address the SoC does not decode **hangs the ZynqMP AXI bus
> with no timeout** — JTAG-POR-only recovery. The cross-die data plane also wedges
> **intermittently even when driven correctly**, so §7 is attended-only.

> ### ⚠️ Honest status
> | Flow | State |
> |---|---|
> | **Homogeneous** eth-chiplet `die_a ↔ die_b` (same design, mirrored strap) | **[PROVEN]** on silicon — link up at FCSM=4 bilaterally and cross-die SRAM + mailbox transfers, `kr260_01`/`kr260_02`, 2026-07-27..29 |
> | **Heterogeneous** eth-chiplet ↔ **compute**-chiplet | **[BLOCKED]** — has never been run and **cannot be yet**. The compute chiplet has no KR260 bitstream. See [`BRINGUP_GAPS.md`](BRINGUP_GAPS.md). |
>
> This runbook is written for the heterogeneous pair. Every step is tagged with
> which of the two it has actually been executed on. **Run it homogeneously
> first** — it is the working control that tells you whether a het failure is the
> bench or the design.

> **Confidence tags.** **[PROVEN]** — this exact command/flow has run on silicon.
> **[FIRST-TIME]** — correct by construction, never run on this pair; treat as a
> debug step, not a formality. **[BLOCKED]** — cannot run; the artefact does not
> exist.

---

## 0. Bill of materials

| Item | Notes |
|---|---|
| 2× KR260 | `kr260_01` (10.22.24.159) / `kr260_02` (10.22.24.153) in fpgahub, both currently free |
| Straight-through RPi-40 ribbon, J21↔J21 | **strip +3V3 (phys 1, 17) and +5V (phys 2, 4)** — a full ribbon back-feeds the regulators |
| 2× 3.3 V SWD probe | ST-Link / DAPLink on **PMOD2** pins 1–3. **Optional** — the PS-side flow needs no probe |
| Dev host | `mapstone-dev` reaches both boards and runs the fpgahub daemon |
| *(ethernet milestone only)* 2× LAN8720 + 2 host NIC ports | not needed for D2D |

**Bitstreams.** Two exist, both eth-chiplet, in
`tidelink/imp/fpga/output/` of the eth-chiplet repo:
`kr260-eth-chiplet/` (die_a, role strap 0) and `kr260-eth-chiplet-flip/` (die_b,
strap 1, mirrored ball map). **There is no compute-chiplet bitstream** — gap G1 in
[`BRINGUP_GAPS.md`](BRINGUP_GAPS.md).

Full physical detail: [`BOARD_WIRING.md`](BOARD_WIRING.md).

---

## 1. Cabling — powered off **[PROVEN]**

1. Ribbon J21↔J21, **straight-through** (`BCM_n ↔ BCM_n`), power rails stripped.
   The *flip build* does the TX/RX crossover, not the cable.
2. **die_a image → one board, die_b (`-flip`) image → the other.** The same image
   on both drives two outputs onto every one of the 18 lanes. Never do it.
3. Confirm **pin-1 orientation on both J21 ends** — a reversed connector puts 5 V
   onto BCM19.
4. Bridge ≥ 4 interleaved grounds (9, 14, 25, 39).
5. *(optional)* SWD probe on PMOD2: `J11=SWCLK(1)  J10=SWDIO(2)  K13=nRST(3)`,
   GND pin 5/11, VREF pin 6/12 (must read 3.3 V).

Then run the pre-power checklist in [`BOARD_WIRING.md`](BOARD_WIRING.md) §8.

---

## 2. Host setup **[FIRST-TIME — this repo is new]**

```bash
source set_env.sh
make deps            # sub-repos + python env
make test-offline    # L0: pure host logic, no boards, no /dev/mem
```

`make test-offline` **must pass before you touch a board.** It exercises the
address-guard maths, the target registry and the safety decorators — the code that
stands between a typo and a wedged board.

Point the framework at your pair via `hetsoc.toml` (or `$HETSOC_CONFIG`):

```toml
[pair.default]
a = "eth"
b = "compute"

[board.eth]
host    = "ubuntu@10.22.24.159"
fpgahub = "kr260_01"
target  = "kr260-eth-chiplet"
role    = "die_a"

[board.compute]
host    = "ubuntu@10.22.24.153"
fpgahub = "kr260_02"
target  = "kr260-compute-chiplet"     # <-- does not exist yet; see BRINGUP_GAPS G1
role    = "die_b"
```

> **To run the homogeneous control instead**, set `board.compute.target` to
> `"kr260-eth-chiplet-flip"`. That is the **[PROVEN]** configuration.

---

## 3. Lease the boards **[PROVEN]**

```bash
fpgahub status      # kr260_01, kr260_02 must show "In use? no"
make lease          # acquires both; records build/lease.env
```

or by hand:

```bash
fpgahub lease acquire kr260_01
fpgahub lease acquire kr260_02
```

In the framework:

```python
from hetsoc import fpgahub
with fpgahub.lease("kr260_01", "kr260_02"):
    ...
```

> Each KR260 appears **twice** in the hub: `kr260_01` (PS/management, lab net
> 10.22.24.x) and `kr260_01_pl` (the PL-ethernet segment, 192.168.x.x). D2D work
> uses only the PS entry. The `_pl` entries matter for the ethernet milestone —
> and they are what breaks the group `board reset` (§10).

Collection endpoints (`status`, `board list`, `lease …`) work from any host.
Per-board endpoints do not — see §10.

---

## 4. Deploy — one image per board **[PROVEN for eth↔eth / BLOCKED for eth↔compute]**

> 🔴 **The wedge hazard defines this step.** The PS reaches the SoC **only**
> through the `eth_ss_0` backdoor at PS phys `0x4_0000_0000`. The bare-link AFI
> canaries (`0x8403_xxxx`) wedged `kr260_01` on first load. The deploy path
> auto-skips them for `kr260-eth-chiplet*` targets (`KR260_AFI_NO_CANARY=1`,
> threaded by `kr260_deploy.sh`); the AFI width fix still runs. Do not re-enable
> them, and do not point any bare-link script at a chiplet board.

The framework way — reflashes **both** dies in the correct order, so you can never
leave a half-loaded pair:

```bash
make deploy-pair
```

Via fpgahub — **one action per role**, one board each:

```bash
fpgahub actions run kr260_01 deploy_eth_die_a     # -> kr260-eth-chiplet      (strap 0)
fpgahub actions run kr260_02 deploy_eth_die_b     # -> kr260-eth-chiplet-flip (strap 1)
```

These live in [`../fpgahub/fpgahub.toml`](../fpgahub/fpgahub.toml) and need
registering once — see [`../fpgahub/README.md`](../fpgahub/README.md) §3.

> ### ⚠️ The ancestor's fpgahub action no longer works
> The eth-chiplet runbook documents
> `fpgahub actions run kr260_01 deploy_kr260_eth_chiplet_pair` as **[PROVEN]**.
> It was — before fpgahub moved to the board/target/link model. Its manifest now
> **fails to load** (`unknown token namespace 'pair' in {pair.local.role}`), and
> **no manifest is bound to `kr260_01` or `kr260_02` at all** today. Verified
> live 2026-07-29; details in [`BRINGUP_GAPS.md`](BRINGUP_GAPS.md) G10.
>
> There is also **no `[links.*]` block joining the two boards**, so `{link.*}`
> tokens do not resolve either — which is why there is one action per role
> rather than one role-aware action.

Or directly, which is what the actions call (**[PROVEN]** — this command line is
unaffected by the fpgahub change):

```bash
make -C tidelink/fpga deploy_pair_role SOC=kr260_eth ROLE=die_a \
     KR260_HOST=ubuntu@10.22.24.159 KR260_PASSWORD=<pw>
make -C tidelink/fpga deploy_pair_role SOC=kr260_eth ROLE=die_b \
     KR260_HOST=ubuntu@10.22.24.153 KR260_PASSWORD=<pw>
```

(from the eth-chiplet repo root; from this repo, `-C deps/eth-chiplet/tidelink/fpga`
after a recursive `make deps`).

Or from the framework — reflashes both, correct order, no half-loaded pair:

```python
pair.bringup(deploy=True)
```

**What to watch.** `fpgautil ... -f Full` returns success and `fpga_manager` state
reads `operating` on both boards. The KR260s run **plain Ubuntu (no PYNQ)**; the
`.bin` is **header-stripped only, NOT byte-swapped** (`bit2bin_zynqmp.py`) — the
Zynq-7000 byte-swapped flavour silently corrupts a ZynqMP load. An **AFI
PS-master-port width re-poke to 32-bit** runs after every PL load; it is not
persisted, so if PS→PL reads later come back wrong-width, that step didn't run.

A full post-load SSH round-trip completing is the proof the PS AXI bus is healthy
rather than wedged.

> ⚠️ **Never PL-reload one side of a live link** ([`SAFETY.md`](SAFETY.md) H5).
> Reload both, or POR both. `role_lock_reg` is W1S with **POR-only** clear, so a
> one-sided reload leaves the peer permanently mis-latched.

---

## 5. Prove each board is alive — per board **[PROVEN]**

Two read-only checks. Both touch only combinational boot ROM / RO APB registers on
the free-running system clock, **inside** the backdoor window, so they cannot wedge
the bus. The SoC's AHB matrix terminates any in-window miss with `SLVERR`, never a
hang.

```bash
make bench-status        # L1: probe both boards, read-only, wedge-safe. START HERE.
make test-single         # L1+L2: adds config-plane writes, one board at a time
```

or per board:

```python
b.alive()          # boot-ROM probe -> proves the PS->SoC backdoor delivers
b.lane_status()    # TideLink config plane: role, cal_done, FCSM
```

**Expected [PROVEN on silicon 2026-07-27]:**

| Check | Address | die_a | die_b |
|---|---|---|---|
| Boot ROM | PS `0x4_0000_0000` | `0x18003C00 / 0x08000189 / …` | same |
| `ROLE_STATUS` | SoC `0x2E03_2084` | `effective_role = 0` (**master**) | `= 1` (slave) |

> `role_status[0]` is **inverted**: the field is `role_effective`, and
> `role_is_master = ~role_effective`. A mirror that copies the bit straight across
> reports the role backwards.
>
> `role_locked` and `link_active` are the **same net** (`assign link_active =
> role_locked_o`). "Link active" means a role was latched, nothing more. Judge link
> health by **FCSM**, never by `link_active` and never by lane-lock.

**Straps wrong here = stop.** If both boards report the same role, you have the
same image on both — power off and reflash before the ribbon carries anything.

> ⚠️ **Never run `kr260_smoke.py` / `kr260_onchip_*.py` / `tl39.py` /
> `kr260_credit_tx.py` / `kr260_drain.py` on a chiplet board.** Their map is
> bare-link: they poke `0x8403_xxxx` / `0x8000_0000` / `0xA400_xxxx`, which are
> **undecoded** here. The read hangs the PS AXI bus with no timeout. This wedged
> `kr260_01` on 2026-07-27. The framework addresses the SoC through the
> `0x4_2E03_xxxx` backdoor and **refuses** any out-of-window address.

---

## 6. Bring the link up — on BOTH boards concurrently **[PROVEN for eth↔eth]**

The link is brought up **entirely from each board's PS over the backdoor** — no
firmware, no SWD probe. Each die's `cal_done` only asserts once the peer is also up
over the ribbon (the forwarded-clock RX calibrates against the peer), so the two
independent runs **self-synchronise**.

```bash
make bench-bringup       # drives BOTH dies concurrently. Fresh dies only.
make test-pair           # L3: verifies the link + the cross-die CONTROL plane
```

or:

```python
pair.bringup(deploy=True)   # concurrent, both dies, fresh after reflash
pair.verify_link()          # read-only FCSM==4 on both
```

The register recipe, on each die (SoC addresses; PS = `0x4_0000_0000 +` these):

| Step | Register | Address | Value |
|---|---|---|---|
| 1 | `ROLE_CFG` — role + lock | `0x2E03_2080` | `0x02` die_a (master-lock) / `0x03` die_b (slave-lock) |
| 2 | poll `SWI_LANE_STATUS` | `0x2E03_2108` | wait `cal_done` = bit[16] |
| 3a | `SWI_TRAINING_MODE` — drop training | `0x2E03_2100` | `0x0` |
| 3b | `WL_LINK_ENABLE_RESET` bootstrap | `0x2E03_0208` | `0x00027F08` → `0x00027F00` → `0x00027F07` |
| 4 | verify `SWI_LANE_STATUS` | `0x2E03_2108` | **FCSM = 4 (LINK_IDLE)**, `cal_done = 1` |

> `ROLE_CFG` is at `0x2080`, **not** `0x2084`. Writing `0x2084` lands on the next
> register and the role never locks.

**Success criterion:** **both** dies at **FCSM = 4 (LINK_IDLE)** with
`calibration_done = 1`, bilaterally.

**Do not judge by lane-lock.** `lanes_locked` reads `0xFF` only while the
calibrator drives training patterns and self-deasserts to `0x00` after `S_DONE` —
`0x00` after training is **expected and healthy**. Likewise `PAIR_CREDIT_COUNTER`
reads 0 on a perfectly healthy link.

**[PROVEN] reference values, 2026-07-27:** `SWI_LANE_STATUS` die_a `0x05890000`,
die_b `0x27890000` — FCSM=4, `cal_done=1`, `cr_seen = crack_seen = 1`, and the link
**held** on a read-only re-check.

> 🔴 **Run bring-up only on FRESH dies.** Re-running it (`LL_SWRESET`) on an
> **already-live** link desyncs it and hangs the sender's peer writes — this wedged
> `die_a` on 2026-07-29. If you want to know whether a running link is up, use
> `verify_link()` (read-only). `bringup(deploy=True)` is safe because the reflash
> makes both dies fresh.

**Role determinism.** die_a is pinned grandmaster by the `ROLE_CFG` master-lock.
Do **not** rely on TideChart auto-election: both dies ship `DEVICE_CLASS = 0x0001`
with no per-die override, which produces a non-deterministic dual-root election.

---

## 7. Cross-die transfer — the data plane 🔴 **ATTENDED ONLY** **[PROVEN for eth↔eth]**

> **This is the step that wedges.** The shipped build has recovery-stripped AXI
> flow-control nodes, so a single bit error has no recovery path and hangs the PS
> bus permanently. The **first** transfer after bring-up reliably passes; each
> subsequent one is progressively more likely to wedge. Observed wedging on ~2 of 3
> repeats, on both a peer read and a peer write. Mechanism and evidence:
> [`SAFETY.md`](SAFETY.md) H3.
>
> **Before the first peer access: open a terminal on `mapstone-dev` with the §10
> recovery command ready.** Never run this unattended, in CI, or overnight.

```bash
I_ACCEPT_WEDGE_RISK=1 make test-dataplane        # L4 — double opt-in
```

**Two gates, deliberately.** The `data_plane` / `soak` markers are *deselected*
unless `--data-plane` is passed, **and** the runner refuses unless
`I_ACCEPT_WEDGE_RISK=1` is in the environment ("*you are the recovery plan*").
The peer **read** round-trip — the single most wedge-prone operation — needs a
third: `--allow-peer-read`.

The transfer, in three parts:

```python
pair.program_cam(pair.a, match=0x2F, replace=0x2D)   # 3 writes; CTRL armed LAST
pair.peer_write(pair.a, 0x2F001000, 0xC0FFEE01)     # requires FCSM==4
pair.b.read(0x2D001000)                              # die-local read on the RECEIVER — safe
```

The address path, and why the CAM is load-bearing:

```
die_a CPU/PS writes 0x2F001000        (peer aperture)
  -> CAM rewrites addr[31:24] 0x2F -> 0x2D      (0x2E03_4010, RULE_0 = 0x002D2F01)
  -> XHB500 AHB->AXI -> Wlink AXI FC nodes -> PHY -> ribbon
  -> die_b WL2AXI -> ahb_mng -> d2d_m initiator
  -> die_b shared_sram_0[0x2D001000]
```

die_b's `d2d_m` initiator reaches **only** `shared_sram_0` (`0x2D`) and
`ipc_mailbox_0` (`0x23`); everything else DECERRs. die_a's own `0x2D` is die_a's
own SRAM, so the address **must** be actively rewritten before it crosses. With the
CAM at its reset state the far die sees `0x2F…` and DECERRs.

**CAM programming — three writes, `CTRL` armed last:**

| Register | Address | Value |
|---|---|---|
| `BASE_OFFSET` | `0x2E03_4000` | `0x00000000` |
| `RULE_0` | `0x2E03_4010` | `0x002D2F01` (enable=1, match=`0x2F`, replace=`0x2D`) |
| `CTRL` | `0x2E03_4004` | `0x00000001` (global_enable) |

> **One aperture reaches one 16 MB region.** The CAM matches/replaces `addr[31:24]`
> only, and the aperture is a single upper byte, so exactly one rule ever fires.
> To reach the mailbox instead, reprogram `RULE_0.replace` to `0x23` — you cannot
> have both mapped at once.
>
> **The CAM does not survive a warm `hresetn`** (`ROLE_CFG` does). After any warm
> reset the link is up but the translator is disabled, and the first peer write
> silently DECERRs. Reprogram it.

**[PROVEN] on-silicon results (eth↔eth):**

| Test | Result |
|---|---|
| `sram_fwd` — die_a → die_b `shared_sram_0[0x2D001000] = 0xC0FFEE01` | **PASS** (die_b local read) |
| `sram_rtt` — die_a reads it back over the link | **PASS** — but this is the fragile one |
| `sram_rev` — die_b → die_a (`0xB2A0FEED`) | **PASS** — slave→master direction works |
| `mailbox` — die_a → die_b `ipc_mailbox_0` (CAM `0x2F→0x23`), slot0 + `MSG_VALID` | **PASS**; `irq_status @ 0x2300_0028` latched, proving the far-die IRQ **source** |
| `soak` — 2000 write+readback beats | **PASS** once: 0 mismatches, FCSM held 4, sticky faults `0x0`, `CREDIT_COUNT` steady 4096, `OBS_FC_CREDIT = 0xFC00001F` |

That soak passing is a **clean-BER window, not proof of recovery.** Repeating the
suite exposed the wedge.

**Delivering an actual ISR needs firmware.** Both cores are boot-gated in the PS
flow; the mailbox IRQ *source* is PS-observable, delivery is not.

---

## 8. Regression **[PROVEN pattern]**

Re-run after every design iteration to confirm a rebuild still works on silicon.

```bash
make regress                                          # L0 -> L1/L2 -> L3. CI-safe.
I_ACCEPT_WEDGE_RISK=1 ./scripts/regress.sh --data-plane   # + L4    ATTENDED
I_ACCEPT_WEDGE_RISK=1 ./scripts/regress.sh --soak         # + L4+L5 ATTENDED
```

Results land in `build/results/`; `make junit` merges them for CI and
`make dashboard` renders a single-file HTML summary.

The layering exists because of the wedge:

| Level | Needs | Wedge risk | CI |
|---|---|---|---|
| L0 | nothing (host logic) | none | yes, always |
| L1 | 1 board, read-only probes | none | yes, nightly |
| L2 | 1 board, config-plane writes | low | yes, nightly |
| L3 | 2 boards, link bring-up + control plane | low | yes, nightly |
| **L4** | 2 boards, **cross-die data plane** | **HIGH** | **no** |
| **L5** | 2 boards, soak / stress | **HIGH** | **no** |

The **default suite is die-local and CI-safe** — deploy, link-up to FCSM=4, backdoor
boot-ROM, role strap, register planes. None of it pushes data across the link.
That is the honest split: register-plane and link-up are a reliable repeatable
gate; the cross-die data plane is not yet CI-stable.

The ancestor's runner (`kr260_eth_regress.py`, 10/10 PASS on a clean deploy
2026-07-29) is the reference for what the suite must cover — but note it only ever
reached 10/10 **once**; repeating it found the intermittent wedge, which is exactly
what a regression is for.

**Always reflash before a full run.** `--deploy` / `bringup(deploy=True)` is the
design-iteration flow *and* the safe one: the link is only ever brought up on fresh
dies (§6).

---

## 9. Teardown

```bash
make release        # releases the lease recorded in build/lease.env
```

or by hand:

```bash
fpgahub lease release kr260_01
fpgahub lease release kr260_02
```

Leave the boards **loaded but idle** — do not POR out of habit; a POR clears the PL
and the next operator gets an unloaded board. Do POR if you wedged anything, if a
run ended in an unknown state, or if the link was left half-brought-up.

Power off before unplugging the ribbon.

---

## 10. Recovery — a wedged board **[PROVEN]**

**Symptom:** the board stops answering ping/SSH mid-run, no error message.

**The framework way**, which encodes both quirks below so you don't have to
remember them mid-incident:

```bash
make bench-recover BOARD=kr260_01     # omit BOARD= to POR both
```

If you are doing it by hand, the two quirks are:

**Quirk A — per-board endpoints 404 from this client host.** `board reset`,
`board show` and `actions` route through `board/<name>/…` and 404 from some clients
(CLI/daemon route skew). They work **on `mapstone-dev`**, where the daemon lives:

```bash
ssh mapstone-dev 'fpgahub board reset kr260_01 --yes'
# -> POR issued ... method=default plugin=kr260_jtag_por, via local (cable ...)
```

**Quirk B — the group `board reset` breaks on the `_pl` topology entry.** Reset the
**single** member through the API socket instead:

```bash
ssh mapstone-dev "curl -s --unix-socket /run/fpgahub/fpgahub.sock \
  -X POST http://localhost/api/v1/targets/kr260_01/reset \
  -H 'Content-Type: application/json' \
  -d '{\"method\":\"default\",\"confirm\":true}'"
```

**POR one board at a time, ~8 s apart.** Back-to-back PORs hit a transient
"cable not found" on the second board — **retry once**; it succeeds.

**Verify it came back** (~10 s later):

```bash
ping -c3 10.22.24.159
ssh ubuntu@10.22.24.159 true     # a full SSH round-trip proves the PS AXI bus is healthy
```

A POR clears the PL — **redeploy both boards** before continuing (§4). Never bring
a half-loaded pair back up.

**If a pair wedges twice on the same test, stop board-hammering.** The intermittent
wedge needs an RTL/timing fix, not more POR cycles. Record what wedged and switch
to die-local work.

---

## 11. Fast-path checklist

```
[ ] ribbon straight-through; phys 1,2,4,17 (+3V3/+5V) stripped; pin-1 both ends
[ ] die_a image on one board, die_b (-flip) on the other — NOT the same image
[ ] make test-offline    (L0) passes BEFORE touching a board
[ ] make lease           both boards free and leased
[ ] make deploy-pair     fpga_manager = operating on BOTH
[ ] make bench-status    boot ROM 0x18003c00... on both (PS->SoC backdoor alive)
[ ]                      role straps read back OPPOSITE (die_a=0, die_b=1)
[ ] make test-single     (L1+L2) green
[ ] ribbon seated
[ ] make bench-bringup   both dies TOGETHER, fresh dies only
[ ] make test-pair       both dies -> FCSM=4 (LINK_IDLE) + cal_done=1  <-- link up
[ ] ---- everything above is wedge-safe. Below is not. ----
[ ] recovery terminal open on mapstone-dev, and you are sat at the bench
[ ] I_ACCEPT_WEDGE_RISK=1 make test-dataplane   (L4, ATTENDED ONLY)
[ ] make release
```

---

## 12. Troubleshooting

### The link will not reach FCSM = 4

Work down this list; each step is cheaper than the one after it.

| # | Check | How | If wrong |
|---|---|---|---|
| 1 | **Are both dies actually running the right image?** | `ROLE_STATUS` `0x2E03_2084` — must read **opposite** effective_role | Same image on both → power off, reflash. This is the single most common failure and it also shorts every lane. |
| 2 | **Did you bring both up concurrently?** | one terminal per board, same window | `cal_done` gates on the peer. A single-die bring-up gets `ROLE_STATUS 0x00→0x02` and **FCSM `0→1`**, then stops with `cal_done = 0` — that is *correct* behaviour, not a fault. |
| 3 | **Did you re-run bring-up on a live link?** | shell history | That desyncs it and hangs peer writes. POR both, redeploy, start clean. |
| 4 | **Is the ribbon actually seated, right way round?** | pin-1 silkscreen, both ends | A reversed connector puts 5 V onto BCM19. |
| 5 | **Are all 18 lanes + 4 grounds bridged?** | continuity-test against [`BOARD_WIRING.md`](BOARD_WIRING.md) §3.2 | A partial loom missing one conductor. Note the 26-way "cannot reach phys 27" theory was **refuted** — do not chase it. |
| 6 | **Was the link brought up on fresh dies?** | did you reflash / POR first? | A stale `role_lock_reg` (POR-only clear) survives everything short of a POR. |
| 7 | **Is `pad_clk_rx` alive?** | `WLINK_LINK_STATUS` `0x2E03_0234`: bit[3] `tx_active`, bit[4] `rx_valid` | These are the only informative bits **not** downstream of role-lock — the right ones for a die whose link won't come up. |
| 8 | **Is it a lottery, not a fault?** | run it 4×, not once | Epoch anchoring on the KR260 is asymmetric (die_b ≫ die_a). **A success is conclusive; a single failure proves nothing.** Do not A/B two builds on one trial each. |
| 9 | **Is it lane 0 / the clock specifically?** | lanes 1–7 clean, lane 0 or clock sick | `BCM0`/`BCM1` are `ID_SD`/`ID_SC` — carrier HAT-ID pull-ups load them. Six spare HDGC balls exist; moving the clock pair is XDC-only + a rebuild. |
| 10 | **Bench or design?** | **fall back to the bare-link control image** | ↓ |

> ### The bare-link fallback — how to split bench from design
>
> `kr260-pair-nptp` / `kr260-pair-flip-nptp` are the **smaller, route-clean
> bare-link** designs (TideLink terminating in a BRAM instead of a whole SoC).
> Loading those isolates *"is the ribbon / PHY / bench flow good?"* from *"is the
> chiplet SoC good?"*.
>
> **If the bare link comes up and the chiplet does not → the problem is the SoC
> integration. If neither comes up → the problem is the bench.**
>
> 🔴 **The bare-link image has a different address map, and its tools
> (`kr260_smoke.py`, `tl39.py`, `bringup_pair_converge.sh`) poke `0x8403_xxxx`.
> Those are correct for the bare-link image and will wedge a chiplet board.** Never
> mix the two tool sets with the two images. Reflash fully when you switch.

### Symptom → cause

| Symptom | Almost always |
|---|---|
| Board stops answering ping/SSH mid-run, no error | Wedge. §10. |
| Both dies report the same role | Same image on both boards |
| `FCSM` stuck at 1, `cal_done = 0` | The peer isn't up. Bring up both together. |
| `lanes_locked = 0x00` after training | **Expected.** Judge by FCSM. |
| `PAIR_CREDIT_COUNTER = 0` | **Expected** on a healthy link. Not a fault. |
| `link_active = 1` but nothing works | `link_active` **is** `role_locked` — it means a role latched, nothing more |
| First cross-die transfer passes, a later one hangs | The known intermittent wedge. [`SAFETY.md`](SAFETY.md) H3. Not your fault, not fixable from the host. |
| Peer write returns cleanly but nothing lands on the far die | CAM not programmed, or cleared by a warm `hresetn`. Reprogram `0x2E03_4000/4010/4004`. |
| PS→PL reads come back wrong-width after a load | The AFI 32-bit width re-poke didn't run. It is required on **every** PL load and is not persisted. |
| PL load "succeeds" but nothing responds | Wrong `.bin` flavour — ZynqMP needs header-stripped, **not** byte-swapped |
| `fpgahub board reset` 404s | Quirk A — run it on `mapstone-dev` (§10) |
| `board reset` fails on the group | Quirk B — reset the single member via the API socket (§10) |
| Second POR says "cable not found" | Transient. Wait ~8 s and retry once. |
| SWD probe won't attach | VREF must read 3.3 V on PMOD2 pin 6; then drop `adapter speed 1000` |

### Diagnostics that do *not* see the failure

Worth knowing so you don't waste a session on them:

- `OBS_FC_CREDIT` (`0x2E03_219C`) and `SWI_LANE_STATUS[31:17]` observe the
  **sideband** FC node only — **not** the AXI data nodes that wedge.
- `RELEASE_THRESHOLD` tuning cannot affect the wedge; the peer window rides the AXI
  transport, not the FIFO/returner sideband.
- `SUB_STALL_TIMEOUT` cannot catch it — a lost response beat parks XHB500 with
  `hreadyout` high, so it is invisible and there is no clean `SIGBUS`.
- `d2d_reset` / `in_error_state` (`0x2E03_0234` bit[2]) is **tied low by
  construction** and can never assert.
- `sync_seen` (`0x2E03_215C`) is **retired under `TIDELINK_PHY_V2` and reads 0
  regardless of link health**. Reading 0 there is not evidence of anything. Use
  `0x2140` (epoch), `0x2120` (TX SYNC-obs), `0x2108` (cal/FCSM) instead.

**What *does* see it** — poll these *between* transfers, before a hang. Node
offsets are within the Wlink bank (`0x2E03_0000`), so e.g. the B node is
`0x2E03_1200` *(derived; confirm against TideLink `REGISTER_MAP.md:448-471` before
relying on it)*:

| Node | Bank offset | `+0x08` | `+0x10` | `+0x20` |
|---|---|---|---|---|
| AW | `0x1000` | TX-FC-FIFO empty | Ack/Nack FIFO full/half/empty | CRC-error count |
| W | `0x1100` | ″ | ″ | ″ |
| **B** (write wedge) | `0x1200` | ″ | ″ | ″ |
| AR | `0x1300` | ″ | ″ | ″ |
| **R** (read wedge) | `0x1400` | ″ | ″ | ″ |

Rising CRC count → a bit error (eye drift). Stuck non-empty Ack/Nack FIFO →
credit/ACK stall. Also watch `SYNC_DET` (`0x2E03_2114` [31:16] `sync_detected_cnt`)
and `lane_fault` for drift.

---

## 13. Known caveats going in

- **First silicon.** Expect PHY-eye / deskew surprises at the ~3 MHz link rate.
- **Timing.** TideLink RX has residual setup (−2.9/−3.3 ns, 4 endpoints) and a
  forwarded-clock TX hold (−22 ns) shared with the bare-link target. It is a
  **runtime-calibrated** forwarded-clock interface, so this is not a blocker — but
  watch it if the link is flaky, and it gates trusting any marginal bench result.
- **Calibration is one-shot.** `calibrated_once_q` latches on first `S_DONE` and
  permanently gates re-trigger; only `SWI_FORCE_RECAL` (W1P, POR-default 0, never
  driven by the FSM) can re-cal. The sampling point is frozen at bring-up — this is
  the intermittency trigger behind H3.
- **No header ECC.** The Hamming(33,24) syndrome checker is a deliberate,
  documented bring-up bypass (`corrupted` hardwired to 0). Payload CRC still
  applies; header-corruption *detection* does not exist. Raised upstream.
- **Cross-die SWD debug is blocked** on the H3 fix — it is poll-heavy over the exact
  AXI nodes that wedge.
- **TideChart election** does not converge (dual-root; `force_root` decoded but
  never consumed; `reset` doesn't clear `election_done`). Register plane is alive
  and is a valid non-gating diagnostic.
- **Ethernet** is a separate milestone: no PHY has ever been fitted, no ethernet
  firmware exists for either chiplet, and the hub needs a PL-ethernet segment per
  board. [`BOARD_WIRING.md`](BOARD_WIRING.md) §6.

---

## 14. Command reference

Verified against the repo's `Makefile` and `tests/conftest.py`. `make help` lists
the full set.

### Setup and offline

| Purpose | Command |
|---|---|
| sub-repos + python env | `make deps` (`deps-full` for nested submodules, `venv` alone) |
| **L0** — pure host logic, no boards | `make test-offline` |
| lint / format | `make lint` · `make fmt` |
| bench sanity check | `make preflight` (`preflight-single` / `preflight-pair`) |

### On the bench

| Purpose | Command | Wedge risk |
|---|---|---|
| lease / release both boards | `make lease` · `make release` | none |
| deploy both dies | `make deploy-pair` | none |
| **L1** read-only probe | `make bench-status` | **none — start here** |
| **L1+L2** single-board suite | `make test-single` | none |
| bring the link up, both dies | `make bench-bringup` | low (fresh dies only) |
| **L3** two-board control plane | `make test-pair` | low |
| **L4** cross-die data plane | `I_ACCEPT_WEDGE_RISK=1 make test-dataplane` | 🔴 **HIGH** |
| **L5** soak | `I_ACCEPT_WEDGE_RISK=1 make test-soak` | 🔴 **HIGH** |
| full CI-safe regression | `make regress` | none |
| POR a wedged board | `make bench-recover BOARD=kr260_01` | destructive (clears PL) |
| merge results / render | `make junit` · `make dashboard` | none |
| pre-silicon sim | `make sim` · `make sim-het-pair` | none |

### pytest markers

`l0`–`l6` map to the levels in §8. Also: `hardware`, `single_board`, `pair`,
`data_plane`, `soak`, `peer_read`, `slow`, `nongating`.

`data_plane` and `soak` are **deselected unless `--data-plane`**; the runner then
*also* requires `I_ACCEPT_WEDGE_RISK=1`. `peer_read` — the most wedge-prone
operation — needs `--allow-peer-read` on top of both.

### fpgahub

| Purpose | Command | Note |
|---|---|---|
| fleet status | `fpgahub status` · `fpgahub board list` | works from any host |
| lease | `fpgahub lease acquire <board>` | works from any host |
| deploy | `fpgahub actions run kr260_01 deploy_eth_die_a` | **needs registering** — [`../fpgahub/README.md`](../fpgahub/README.md) §3 |
| deploy (direct, **[PROVEN]**) | `make -C tidelink/fpga deploy_pair_role SOC=kr260_eth ROLE=…` | ancestor runbook §3 |
| POR | `ssh mapstone-dev 'fpgahub board reset <board> --yes'` | verified 2026-07-27 |
| POR (single member) | curl to the unix socket, §10 | verified 2026-07-29 |

> `host/hetsoc/` and `Makefile` are owned by other areas. The Python snippets
> above follow [`../host/API_CONTRACT.md`](../host/API_CONTRACT.md); there is **no
> `hetsoc` console-script entry point** in that contract, so the CLI surface is
> `make` plus `./scripts/regress.sh`.

---

## References

- [`SAFETY.md`](SAFETY.md) — hazards, forbidden tools, recovery
- [`BRINGUP_GAPS.md`](BRINGUP_GAPS.md) — what blocks the heterogeneous pair
- [`BOARD_WIRING.md`](BOARD_WIRING.md) — ribbon, PMODs, ball maps
- [`../fpgahub/README.md`](../fpgahub/README.md) — lab-tool manifest and actions
- Ancestor runbook (the proven eth↔eth flow): eth-chiplet `docs/KR260_BENCH_RUNBOOK.md`
- Wedge root cause: eth-chiplet `docs/CROSS_DIE_WEDGE_ROOTCAUSE.md`,
  `docs/TIDELINK_SILICON_FEEDBACK.md`
- Register/address authority: eth-chiplet `docs/STATUS_REGISTERS.md`,
  `docs/PEER_APERTURE_PROGRAMMING.md`, `docs/CROSS_DIE_INTERRUPTS.md`
- Reset ordering and the two-die power-up hazard: eth-chiplet `docs/RESET_ORDERING.md`
