# `hetsoc` — host test framework for the NanoSoC heterogeneous chiplet pair

The layer every on-silicon test drives. Two Xilinx KR260 boards, each running one
chiplet bitstream, joined by a J21 ribbon carrying the TideLink die-to-die
interface. `hetsoc` generalises the ad-hoc bench scripts that proved the
*homogeneous* eth↔eth pair on silicon (2026-07-27…29) into a design-agnostic
framework — and turns the safety rules that were runbook prose into **code**.

```bash
pip install -e host                 # no runtime dependencies
cp hetsoc.toml.example hetsoc.toml  # edit the two board addresses
hetsoc targets                      # the address registry (no boards needed)
hetsoc status                       # read-only, both dies
hetsoc verify                       # read-only: is the link up?
hetsoc health                       # the wedge diagnostic
```

---

## The one thing to understand

The KR260 PS reaches the chiplet SoC **only** through a narrow backdoor window
(`PS phys = 0x4_0000_0000 + soc_addr` on the eth chiplet). **Any PS read of a PL
address the SoC does not decode hangs the ZynqMP AXI bus with no timeout** — the
board drops to 100 % packet loss and only a JTAG POR recovers it. That is not
hypothetical: the bare-link AFI canaries at `0x8403_xxxx` wedged `kr260_01` on
first load.

Everything in the design below follows from that single fact.

| Hazard | Where it is enforced | What you get |
|---|---|---|
| Out-of-window address | `Target.to_host()` | `AddressGuardError` **before** anything is issued |
| Bare-link address on a chiplet target | `Target.__post_init__` invariant | *structurally impossible* — see below |
| Peer (`0x2F`) access on a down link | `Board.read`/`Board.write` → `require_link_up()` | `LinkDownError` |
| A hang | `run_guarded()` around every board op | `WedgeDetected`, never a block |
| Re-bring-up of a live link | `ChipletPair.bringup()` freshness gate | refusal with the recovery recipe |
| Aiming at a third inbound region | `Target.inbound_byte` / `program_cam` | `AddressGuardError` |
| A guessed window base | `ProvisionalTargetError` | refusal, with the TOML escape hatch |

### Why the bare-link trap is *structurally* unreachable

A chiplet backdoor is an HPM0_FPD **high** aperture, so its window base is
required by construction to be ≥ 2³². Every address `to_host()` can emit for a
chiplet target is therefore ≥ 2³² and **cannot** be a 32-bit bare-link address.
It is arithmetic, not a check that can be forgotten. `Target.__post_init__`
refuses to *build* a chiplet descriptor with a sub-4 GiB window;
`_assert_no_bare_link_trap()` asserts the property anyway; and
`tests_unit/test_targets.py::TestNeverEmitsBareLinkAddresses` sweeps every
target over every 16 MB boundary to prove it.

### There is no unchecked path to `/dev/mem`

`Board` exposes no raw accessor. Addresses pass the target guard on the host,
and the on-board agent is started with its window and **refuses anything outside
it a second time**. Two independent guards, one unrecoverable hazard.

---

## Modules

| Module | What it owns | Touches hardware? |
|---|---|---|
| `hetsoc.safety` | the exception hierarchy, `require_link_up`, `guarded`/`run_guarded` | no |
| `hetsoc.regs` | shared SoC-internal register **offsets**, `cam_rule()`, `decode_lane_status()`, the FC-node table | no |
| `hetsoc.targets` | `Target`, `TARGETS`, `get_target()` — the address-descriptor registry and its guards | no |
| `hetsoc.config` | `hetsoc.toml` loading, board/pair factories, `[target.*]` overrides | no |
| `hetsoc._toml` | TOML via `tomllib` → `tomli` → a documented subset parser | no |
| `hetsoc.log` | structured `key=value` logging | no |
| `hetsoc.transport` | `MemoryTransport` (L0), `SshAgentTransport` (default), `SshOneShotTransport` (fallback) | lazily |
| `hetsoc.agent` | the on-board `/dev/mem` agent — stdlib only, runs on the board | on the board |
| `hetsoc.board` | `Board` — the guarded choke point for all access | lazily |
| `hetsoc.pair` | `ChipletPair` — concurrent bring-up, CAM, cross-die data plane, soak | lazily |
| `hetsoc.health` | link + per-node FC health, the known-wedge diagnostic | lazily |
| `hetsoc.fpgahub` | lease/status/JTAG-POR, with both documented fpgahub quirks handled | lazily |
| `hetsoc.cli` | `hetsoc` / `python -m hetsoc` | lazily |

`import hetsoc` works on a machine with **no board, no network, no `/dev/mem`
and no third-party packages** — asserted by
`tests_unit/test_package.py::TestImportContract`. That property is what makes
the L0 tier runnable in any CI container.

---

## Register offsets are offsets, not absolute addresses

`CHIPLET_HOST_TOOLING_PLAN.md`'s central finding is *"base is per-target, offset
is shared"*. So:

```python
board.reg_read(regs.SWI_LANE_STATUS)          # preferred — unambiguous
target.to_host(regs.TLAPB_BASE + regs.SWI_LANE_STATUS)   # the plan's shape
```

`regs.SWI_LANE_STATUS` is `0x2108`, **not** `0x2E032108`. Passing a bare offset
to `Board.read()` reads a different in-window address — harmless (the SoC AHB
matrix's default slave SLVERRs; it cannot hang) but silently wrong. Use
`board.reg(offset)` / `board.reg_read(offset)` and the ambiguity cannot arise.
`hetsoc regs` prints the whole table with both forms.

---

## The heterogeneity that will bite

The two dies do **not** share an address map:

| Region | eth chiplet | compute chiplet |
|---|---|---|
| `shared_sram_0` | `0x2D` | `0x2D` |
| `ipc_mailbox_0` | **`0x23`** | **`0x2A`** |

An eth→compute mailbox write therefore needs CAM `RULE_0 = 0x002A2F01`, not the
`0x00232F01` that is correct for the proven eth↔eth pair. A rule built from the
*sender's* map aims at a region the receiver does not decode; the far die
DECERRs, the response never returns, and on current silicon that is exactly how
the cross-die path wedges the PS bus.

So the CAM rule is always built from **both** descriptors:

```python
pair.map_peer_to(eth_board, "ipc_mailbox")   # 0x00232F01 vs eth, 0x002A2F01 vs compute
```

`ChipletPair.program_cam()` validates the raw form against the **receiving**
board's inbound set and refuses anything else (pass `allow_unmapped=True` for the
deliberate confinement test, with a JTAG POR staged).

---

## The wedge diagnostic

`docs/CROSS_DIE_WEDGE_ROOTCAUSE.md`: the silicon build ships the **upstream,
recovery-stripped** FCSM on the five AXI data-plane flow-control nodes
(AW/W/B/AR/R); only the TideLink sideband node keeps the SoC-Labs recovery
logic. One bit error or dropped ACK on an AXI node has no recovery path and
wedges the bus.

> **`OBS_FC_CREDIT` and `SWI_LANE_STATUS[31:17]` observe the SIDEBAND node only.
> They do NOT see the nodes that wedge.** A run that polls only those reports a
> perfectly healthy link right up to the moment the board dies.

`hetsoc.health` reads the per-node registers that *do* see them — rising CRC on
B/R means a bit error (calibration drift, marginal eye); a stuck non-empty
Ack/Nack FIFO on an AXI node means a credit/ACK stall. All reads are RO and
in-window, so it is wedge-safe and runs at L1 on a single board.
`ChipletPair.soak(..., stop_on_degrade=True)` implements fix #2 of that
document's fix path: stop on the signal rather than let the next transaction
wedge the bus.

---

## Transport

The existing scripts pay a full ssh round-trip per 32-bit access. `hetsoc`
defaults to **one persistent SSH connection driving one long-lived on-board
agent** — one line per access over an already-open pipe. A 21-register FC-health
sample drops from ~4 s of ssh to ~20 ms, which is what makes between-transfer
polling viable at all.

Auth mirrors `kr260_eth_run.sh` / `kr260_deploy.sh` exactly:

| Situation | Login | On-board privilege |
|---|---|---|
| `$HETSOC_PASSWORD`/`$KR260_PASSWORD` set, `sshpass` present | `sshpass -e ssh` (password via `$SSHPASS`, not `ps`) | `sudo -S` |
| password set, no `sshpass` | key auth | `sudo -S` |
| no password | key auth | `sudo -n` (needs NOPASSWD) |

`sudo -S` consumes exactly the first line of stdin and the agent inherits the
rest, which is how one pipe carries both the credential and the protocol.

Set `HETSOC_TRANSPORT=ssh-oneshot` to drop to the no-moving-parts fallback
(shape-identical to today's proven scripts), or `memory` for a fake board.

---

## fpgahub — two documented quirks, both handled

1. **Per-board endpoints 404 from this host.** `board reset`, `board show` and
   `actions run` are routed through `ssh mapstone-dev`; collection endpoints
   (`status`, `lease …`) run locally. Override the daemon host with
   `HETSOC_FPGAHUB_HOST`, or set `HETSOC_FPGAHUB_LOCAL=1` when already on it.
2. **The group `board reset` breaks on the `_pl` topology entry.** `reset()`
   posts the documented **single-member** reset straight at the daemon's unix
   socket, and falls back to the CLI form only if that fails.

```python
with hetsoc.fpgahub.lease("kr260_01", "kr260_02"):
    pair.verify_link()          # leases released even if this raises
```

---

## Bring-up ordering — the rule that costs a bench session

Re-running the bring-up (`LL_SWRESET`) on an **already-live** link desyncs it and
hangs the sender's peer writes. So `ChipletPair.bringup()` refuses unless both
dies are **fresh** — straight out of a deploy or a JTAG POR — or `force=True` is
passed explicitly. `verify_link()` is the read-only alternative and is what a
session with a live link should call.

Bring-up is **concurrent** by construction: `cal_done` only asserts once both
dies have role-locked and the forwarded-clock RX has trained against the peer
over the ribbon, so two sequential bring-ups can never converge.

---

## Config

`$HETSOC_CONFIG` → `./hetsoc.toml` → `~/.config/hetsoc.toml`. **First hit wins,
no merging** — a half-merged address map is more dangerous than a missing one.
See `hetsoc.toml.example` at the repo root. `[target.*]` is the per-design
override; it is the only supported way to give a target a different window, and
it refuses to de-provisionalise a target whose `source` still says TBD.

---

## Tests

```bash
cd host && PYTHONPATH=. python3 -m pytest tests_unit -q     # 305 tests, ~3 s
```

`tests_unit/` is the framework's own L0 suite: no hardware, no network, no
third-party packages beyond pytest. It covers the address maths, the registry
guards, the CAM encoder, the lane-status decoder, the peer gate, the timeout
guard, the agent's window check, the fpgahub command shapes and the
"never emits a bare-link address" property.

It has been **mutation-tested**: 19 deliberate defects injected into the guards
(peer gate removed, window bound removed, CAM bytes transposed, freshness gate
removed, the 4 GiB invariant removed, `link_up` dropping `cal_done`, CAM `CTRL`
armed first, the agent's window check removed, …) are each caught by at least one
test. The suite is not decorative.

`tests/` at the repo root is the on-silicon suite (L1–L5) and is owned
separately; it consumes this package through `host/API_CONTRACT.md`.

---

## Known TBDs

The compute chiplet has **no FPGA/KR260 port** — no bitstream, no `.hwh`, and no
external AHB pad group on its chip boundary at all. `kr260-compute-chiplet`
therefore ships with `window_size = 0`, so `to_host()` raises
`ProvisionalTargetError` on every call. Its SoC-internal map *is* carried (cited,
and needed by the heterogeneous CAM rules); its host window, peer aperture and
boot-ROM signature are TBD. `hetsoc targets kr260-compute-chiplet` prints the
full list. Do not paper over it by copying the eth chiplet's `0x4_0000_0000`.

---

*Copyright (C) 2026, SoC Labs (www.soclabs.org)*
