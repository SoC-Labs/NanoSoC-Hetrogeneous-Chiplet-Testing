# SAFETY — read this before you touch a board

Short on purpose. Everything here has cost someone a bench session.

> **The one rule.** The KR260 PS reaches the chiplet SoC **only** through a narrow
> backdoor window. **Any PS read of a PL address the SoC does not decode hangs the
> ZynqMP AXI bus with no timeout.** The board goes to 100 % packet loss — no SSH, no
> ping, no SIGBUS, no kernel message. **Only a JTAG POR recovers it.** This is not
> hypothetical: it wedged `kr260_01` on 2026-07-27.

Related: [`BENCH_RUNBOOK.md`](BENCH_RUNBOOK.md) (the operator flow),
[`BRINGUP_GAPS.md`](BRINGUP_GAPS.md) (why the het pair cannot run yet),
[`BOARD_WIRING.md`](BOARD_WIRING.md) (the physical bench).

---

## 1. The address model — what "in window" means

On the `kr260-eth-chiplet` / `-flip` bitstreams the SoC hangs off the ZynqMP
**HPM0_FPD high aperture**:

| | |
|---|---|
| PS window | `0x4_0000_0000 .. 0x4_FFFF_FFFF` (`tidelink.hwh:4112`, `eth_ss_0` MEMRANGE) |
| Mapping | SoC HADDR `A` → PS phys `0x4_0000_0000 + A` (clean base-strip) |
| **Not** `0x8000_0000` | the tcl comments and the bare-link notes say `0x8000_0000`; on this build that is **undecoded PL**. |

Everything the framework touches lives inside that window:

| SoC address | What | Plane |
|---|---|---|
| `0x2E03_0000` | Wlink chiplet-controller regs (bank 0, 8 KB) | config |
| `0x2E03_2000` | TideLink config + status (bank 1, 8 KB) | config |
| `0x2E03_4000` | Address-translator CAM, channel 0 | config |
| `0x2F00_0000 .. 0x2FFF_FFFF` | **peer aperture — the cross-die data plane** | **data** |
| `0x2D00_0000` | `shared_sram_0` (inbound D2D target on the far die) | data |
| `0x2300_0000` | `ipc_mailbox_0` (2nd inbound D2D target) | data |
| `0x0000_0000` | boot ROM (the aliveness probe) | read-only |

**In-window undecoded accesses are safe.** The SoC's `eth_ss` AHB matrix has a CMSDK
default slave on the free-running system clock: an in-window miss returns
`HREADY=1 + SLVERR`, never a hang. It is the **out-of-window** PL access — no
SmartConnect responder at all — that hangs forever.

---

## 2. Hazard list

### 🔴 H1 — Out-of-window PL reads. **Hangs the bus. Always.**

Bare-link tooling addresses TideLink at `0x8403_xxxx` / `0x8404_xxxx` /
`0x8000_0000` / `0xA400_xxxx`. On a chiplet bitstream those are undecoded. The PS
`M_AXI_GP0` SmartConnect issues the read, nothing responds, and it never times out.

**Forbidden on any chiplet board — no exceptions, no "just a read":**

```
kr260_smoke.py          kr260_onchip_smoke.py     kr260_onchip_soak.py
kr260_onchip_autonomy.py  tl39.py                 tl_socmap.py
kr260_credit_tx.py      kr260_drain.py            kr260_data_rx.py
bringup_pair_converge.sh  sw_coord_autocal_region8.sh
raw devmem/busybox at 0x8403_xxxx / 0x8404_xxxx / 0xA400_xxxx
```

These are the **bare-link** (`kr260-pair-*`) tools. They are correct for that
image and lethal for this one. Also: the AFI canary poke in `kr260_afi.sh` reads
`0x8403_xxxx` — the deploy path suppresses it for `kr260-eth-chiplet*` targets via
`KR260_AFI_NO_CANARY=1`; the width fix still runs. Do not re-enable it.

### 🔴 H2 — Peer access on a down link. **Hangs the bus.**

A write or read to the peer aperture (`0x2F..`) when the local TideLink is not at
`FCSM = 4` never gets a `B`/`R` beat back. Same saturation, same hang.
`require_link_up()` exists for exactly this.

### 🔴 H3 — The cross-die data plane wedges *intermittently even when used correctly*.

**This is the big one, and it is a silicon defect, not an operator error.**

The shipped FPGA build resolves the five AXI data-plane flow-control nodes
(`WlinkGenericFCSM{,_1,_2,_3,_4}` = AW/W/B/AR/R) to the **upstream, recovery-stripped**
copies. Only the sideband node `FCSM_6` keeps the SoC-Labs recovery logic
(`socl_reack`, state-7 watchdog, L9b/L9c gap re-anchor). Mechanism:

```
one bit error / dropped ACK on an AXI data node
  -> no recovery path: fe_rx_ptr never advances
  -> credit ring fills, fe_rx_is_full latches
  -> node stops emitting; far side's B (write) / R (read) beat never returns
  -> PS M_AXI_GP0 SmartConnect saturates
  -> every PL slave wedges. JTAG POR only.
```

The trigger is **one-shot calibration + a marginal eye**: `calibrated_once_q`
latches on first `S_DONE` and permanently gates re-trigger, so the sampling point
is frozen at bring-up. The **first** cross-die transfer reliably passes; each
**subsequent** one is progressively more likely to sample one bit wrong. Observed
on 2026-07-29: wedged on ~2 of 3 repeats, on a peer *read* once and a peer *write*
(die_b→die_a) once — direction and access type are both irrelevant.

Full analysis: eth-chiplet `docs/CROSS_DIE_WEDGE_ROOTCAUSE.md`. Upstream fix
request: `docs/TIDELINK_SILICON_FEEDBACK.md` P1.

> **Consequence: all cross-die data-plane testing is ATTENDED-ONLY (L4/L5).**
> It never runs unattended, never in CI, never overnight. A wedged pair needs a
> human with the recovery procedure in §5.
>
> `RELEASE_THRESHOLD` tuning **cannot** fix this — the peer window rides the AXI
> transport, not the FIFO/returner sideband that those registers control
> (`CREDIT_COUNT` reads a steady 4096 = idle FIFO throughout).

### 🔴 H4 — Never re-run bring-up on a live link.

`LL_SWRESET` (`0x2E03_0208` bit[3] and the `0x00027F08 → 0x00027F00 → 0x00027F07`
bootstrap) on an **already-live** link desyncs it and hangs the sender's peer
writes. This wedged `die_a` on 2026-07-29.

**Bring-up is only ever safe on fresh dies** — i.e. immediately after a PL reload
or a POR. Any flow that "makes sure the link is up" must **verify** (read
`SWI_LANE_STATUS`, check FCSM==4) and never re-drive. `ChipletPair.bringup()` is a
deploy-then-bring-up operation; if you want to check a running link use
`verify_link()`.

### 🔴 H5 — Never PL-reload one side of a live link.

Reloading one board's bitstream while the peer holds a live link leaves the peer's
recovered-clock domain, credit ring and role-lock latch in a state that nothing
clears (`role_lock_reg` is W1S with **POR-only** clear). Reload **both**, or POR
both. The regression's `--deploy` mode does this correctly: reflash both → bring up
both → test.

### 🟠 H6 — Same image on both boards. **Electrical.**

`die_a` = `kr260-eth-chiplet` (role strap 0), `die_b` = `kr260-eth-chiplet-flip`
(strap 1, mirrored ball map). The ribbon is straight-through `BCM_n ↔ BCM_n`, so
the flip build is what makes each conductor one driver against one receiver. The
same image on both **drives two outputs onto every one of the 18 lanes.**

### 🟠 H7 — A full 40-way ribbon. **Electrical.**

Bridging `+3V3` (J21 phys **1, 17**) or `+5V` (phys **2, 4**) ties two
independently-regulated supplies together and can back-feed a regulator on both
boards. Strip those four conductors or use a partial loom. See
[`BOARD_WIRING.md`](BOARD_WIRING.md) §3.

### 🟡 H8 — The CAM does not survive a warm reset.

The address-translator registers reset on `hresetn`; `ROLE_CFG` / `role_locked`
reset only on POR. So a warm `hresetn` leaves the link up and the translator
**disabled** — and the first peer write then DECERRs on the far die. Reprogram the
CAM (`0x2E03_4000` / `0x4010` / `0x4004`) after any warm reset.

---

## 3. What is SAFE

These cannot wedge the bus and are the whole of L0–L3.

| Safe operation | Why |
|---|---|
| **Boot-ROM aliveness probe** (SoC `0x0000_0000`, PS `0x4_0000_0000`) | combinational ROM on the free-running system clock; in-window |
| **Read-only TideLink config plane** (`0x2E03_0xxx`, `0x2E03_2xxx`) | RO APB registers on `sys_hclk`; **readable with the link DOWN** — the `tx_open = link_active` gate applies only to the TX aperture (`blk == 4'h0`), not to `a_tlapb` |
| **Config-plane writes** — `ROLE_CFG`, CAM rules, `PERF_CTRL` | local APB writes, terminate locally, never cross the link |
| **Bring-up on FRESH dies** (post-deploy / post-POR) | proven flow; see H4 for the one way to get it wrong |
| **Local reads on the receiving die** (`0x2D00_xxxx`, `0x2300_xxxx`) | die-local SRAM/mailbox reads — the *receiver* checking what landed never crosses the link |
| **TideChart register plane** (`0x2E04_xxxx`) | local APB |
| `fpgahub status`, `board list`, `lease …` | collection endpoints, work from any host |

That set is enough to prove: both boards alive, both role straps correct, the link
at FCSM=4 bilaterally, and the far die's mailbox IRQ *source* latched — all without
a single wedge-capable access.

## 4. What is ATTENDED-ONLY

| Operation | Level | Why |
|---|---|---|
| Peer write (`0x2F..` → far `0x2D..` / `0x23..`) | L4 | H3 |
| Peer read / round-trip readback | L4 | H3 — worse; the `rd_pipe_r` read-completion guard is absent from the shipped `tidelink_top` |
| Cross-die soak / stress / characterisation | L5 | H3, sustained |
| Cross-die SWD debug | — | **blocked** on the H3 fix; it is poll-heavy over the exact nodes that wedge |

L4/L5 are opt-in behind an explicit flag and **must not** run in CI. When you do run
them: have a terminal open on `mapstone-dev` with the §5 recovery command ready
*before* the first peer access.

---

## 5. Recovery — a wedged board

**Symptom:** the board stops answering ping/SSH mid-run. There is no error message,
because the CPU issuing the read never returns.

### Step 1 — POR it

JTAG POR via fpgahub (`kr260_jtag_por` plugin). **Two quirks, both mandatory:**

**Quirk A — per-board endpoints 404 from this client host.** `board reset`,
`board show` and `actions` hit `board/<name>/…` routes that 404 from some clients
(a CLI/daemon route skew). They work **on `mapstone-dev`**, where the daemon lives.
Collection endpoints (`status`, `board list`, `lease …`) work from anywhere.

```bash
ssh mapstone-dev 'fpgahub board reset kr260_01 --yes'
# -> "POR issued ... via local (cable ...)"  method=default plugin=kr260_jtag_por
```

**Quirk B — the group `board reset` breaks on the `_pl` topology entry.** Each
KR260 appears twice in the hub (`kr260_01` = PS/management, `kr260_01_pl` = the PL
ethernet segment). A group reset trips over the `_pl` member. Reset the **single**
member through the API directly:

```bash
ssh mapstone-dev "curl -s --unix-socket /run/fpgahub/fpgahub.sock \
  -X POST http://localhost/api/v1/targets/kr260_01/reset \
  -H 'Content-Type: application/json' \
  -d '{\"method\":\"default\",\"confirm\":true}'"
```

**POR one board at a time, with ~8 s between them.** Back-to-back PORs hit a
transient "cable not found" on the second board. That failure is transient —
**retry once** before assuming anything is wrong.

### Step 2 — verify it came back

```bash
ping -c3 10.22.24.159            # ~10 s after the POR
ssh ubuntu@10.22.24.159 true     # a full SSH round-trip proves the PS AXI bus is healthy
```

A POR clears the PL, so the board comes back with **no bitstream loaded**. Redeploy
both boards before doing anything else — never bring a half-loaded pair back up
(H5).

### Step 3 — do not hammer the boards

If a pair wedges twice on the same test, **stop**. H3 needs an RTL/timing fix, not
more POR cycles. Record what wedged (test id, direction, read vs write) and move on
to die-local work.

---

## 6. How the framework enforces this in code

Guards live in `hetsoc.safety` and `hetsoc.targets`. They are not advisory.

| Guard | Contract | Enforces |
|---|---|---|
| `Target.to_host(soc_addr) -> int` | raises `AddressGuardError` outside `[window_base, window_base+window_size)` | **H1**. There is no unchecked path to `/dev/mem`. Every `Board.read`/`write` goes through it. |
| `require_link_up(board)` | raises `LinkDownError` unless `FCSM == 4` | **H2**. Any peer-aperture access (`Target.is_peer()` true) must call it first. |
| `guarded(timeout_s)` | decorator; a hang raises `WedgeDetected` | **H3**. Every board operation is timeout-wrapped, so a wedge surfaces as an exception instead of a hung runner. |
| `Board.alive()` | boot-ROM probe only | never wedges — safe to call on a suspect board |
| `ChipletPair.verify_link()` | read-only FCSM check on both dies | **H4**. The non-destructive alternative to `bringup()`. |
| `data_plane` / `soak` markers | **deselected** unless `--data-plane` | **H3**. The data plane cannot run by accident. |
| `I_ACCEPT_WEDGE_RISK=1` | the runner refuses L4/L5 without it — *"you are the recovery plan"* | **H3**. A second, independent gate, so `--data-plane` in a stale shell alias is not enough. |
| `--allow-peer-read` | a **third** gate on the peer read round-trip | **H3**. The single most wedge-prone operation gets its own opt-in. |
| `Board.por()` / `make bench-recover` | drives the §5 path | Quirks A and B, so operators don't have to remember them |

**Do not add an escape hatch.** If a test needs an address outside the window, the
target descriptor is wrong — fix `TARGETS`, not the guard. Every wedge so far came
from a tool that addressed the chip through the wrong map.
