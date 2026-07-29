# Test matrix — heterogeneous eth ↔ compute chiplet pair

Every test, its id, what it proves, and whether it has ever run.

> ### ⚠️ IDs IN THIS FILE DO NOT YET MATCH THE PYTEST IDS  **[2026-07-29]**
>
> This matrix and `tests/` were written concurrently and chose **different area
> namespaces**. This file uses **one area per functional topic** (`L2-CAM`,
> `L2-ROLE`, `L2-TC`, `L2-LINK`); `tests/` uses **one area per file** (`L2-CFG-01..09`),
> which is what [`REPO_LAYOUT.md`](REPO_LAYOUT.md)'s `tests/test_l<N>_<area>.py`
> ↔ `L<N>-<AREA>-<NN>` rule actually prescribes.
>
> **The dangerous part, now measured** (`scripts/test_id_map.py`, 2026-07-29):
> **23 id strings exist in both namespaces, and all 23 describe different
> tests.** The false-match rate is 100% — not one shared id means the same
> thing. Examples:
>
> | id | plan says | implementation says |
> |---|---|---|
> | `L1-PROBE-03` | board reachable (ssh + sudo + `/dev/mem`) | effective role matches the role deployed |
> | `L3-LINK-05` | role asymmetry on silicon | both dies have seen CR and CRACK packets |
> | `L3-LINK-01` | **het link comes up** | both dies converge to FCSM=4 with `cal_done=1` |
> | `L5-SOAK-03` | bidirectional soak | no sticky fault latches during or after the soak |
>
> So an id that looks like a match **is** a false match. Never resolve one
> namespace against the other; always say which you mean.
>
> **→ [`TEST_ID_MAP.md`](TEST_ID_MAP.md) is the authoritative cross-reference.**
> It is generated (`make test-id-map`) and CI fails if it goes stale, so it
> cannot silently drift the way this warning could.
>
> Treat matrix ids as the *planning* namespace (139 rows, a superset including
> tests not yet written) and pytest ids as the *implementation* namespace (55
> implemented). Area-level mapping:
>
> | Matrix areas | pytest area |
> |---|---|
> | `L0-ADDR`, `L0-TGT`, `L0-REGS`, `L0-SAFE`, `L0-CFG` | `L0-ADDR-01..18` |
> | `L1-PROBE`, `L1-HEALTH`, `L1-LINK`, `L1-TC` | `L1-PROBE-01..09` |
> | `L2-CAM`, `L2-ROLE`, `L2-TC`, `L2-LINK` | `L2-CFG-01..09` |
> | `L3-LINK`, `L3-TC`, `L3-CAL`, `L3-HEALTH` | `L3-LINK-01..08`, `L3-TCHART-01..04` |
> | `L4-SRAM`, `L4-MBOX`, `L4-CONF`, `L4-IRQ` | `L4-DATA-01..09` |
> | `L5-SOAK`, `L5-PERF`, `L5-CHAR`, `L5-RECOV` | `L5-SOAK-01..06` |
> | `L4-DMA`, `L4-PTP`, `L4-IRQ`, `L4-ETH` (blocked items) | `L6-FUTURE-01..05` |
>
> **Open decision:** `REPO_LAYOUT.md` defines only L0–L5, so this matrix is
> arguably right to file the firmware-blocked items under L4 and
> `tests/test_l6_future.py` should fold into L4/L5 — a rename, not a rewrite.
> Deferred to whoever owns the next pass rather than churned unilaterally.

Read with [`ARCHITECTURE.md`](ARCHITECTURE.md) (address facts, DUT asymmetries)
and [`VERIFICATION_PLAN.md`](VERIFICATION_PLAN.md) (strategy, milestones, risk).

Read with [`ARCHITECTURE.md`](ARCHITECTURE.md) (address facts, DUT asymmetries)
and [`VERIFICATION_PLAN.md`](VERIFICATION_PLAN.md) (strategy, milestones, risk).

---

## Conventions

**Id:** `L<N>-<AREA>-<NN>`, `N` = level, `AREA` maps to a pytest file:
`tests/test_l<N>_<area>.py`. Ids are **never recycled** — a retired test keeps its
id with the row struck through in the "Retired" section.

**Status vocabulary** — the honest part of this document:

| Tag | Meaning |
|---|---|
| `PROVEN-HOM` | Passes **on silicon** today — but on the **homogeneous** eth↔eth pair (die_a + die_b of the same design). Porting to the het pair is expected, not proven. |
| `FLAKY-HOM` | Runs on the homogeneous pair but **intermittently wedges the board** (`RISK-1`). Attended only. |
| `FAILED-HOM` | Has been run on the homogeneous pair and **did not pass**. |
| `PROVEN-SIM` | Passes in a committed cocotb/VCS env in one of the source repos. The env is named. |
| `PLANNED` | No blocker except writing it. |
| `BLOCKED-<gap>` | Cannot run until a named gap closes. |

> **Nothing in this matrix has ever run on the heterogeneous pair.** Every
> `PROVEN-*` tag is evidence that the *mechanism* works, not that the *het pair*
> works.

**Gap ids** (full definitions in [`VERIFICATION_PLAN.md` §7](VERIFICATION_PLAN.md)):
`G-FPGA` no compute KR260 port · `G-WEDGE` recovery-stripped AXI FCSMs ·
`G-ADDR` compute address map unconfirmed · `G-TB` het sim testbench not built ·
`G-FW` needs firmware on a boot-gated core · `G-TC` TideChart election/enum RTL gaps ·
`G-PTP` compute PHC exports no live time · `G-SEC` inbound debug security gate not built ·
`G-WIN` compute PS backdoor window unknown.

**Wedge column:** `none` / `low` / **`HIGH`** — probability this test can hang the
ZynqMP PS AXI bus (JTAG-POR-only recovery). Anything **HIGH** is attended-only and
never runs in CI.

**Row count: 139** — L0 23 · L0-SIM 18 · L1 17 · L2 16 · L3 24 · L4 28 · L5 13.
Verify with:
`grep -oE '^\| \`L[0-9]-[A-Z]+-[0-9]{2}\`' docs/TEST_MATRIX.md | sort -u | wc -l`

---

## L0 — offline host logic  ·  no board, no simulator  ·  CI: always  ·  23 rows

`tests/test_l0_{addr,regs,targets,safety,config}.py`

| ID | Name | Proves | Method | Prereq | Pass criteria | Wedge | CI | Status |
|---|---|---|---|---|---|---|---|---|
| `L0-ADDR-01` | eth window mapping | `to_host()` is `window_base + soc_addr` | `get_target("kr260-eth-chiplet").to_host(0x2E032108)` | none | `== 0x4_2E03_2108` | none | yes | PLANNED |
| `L0-ADDR-02` | compute window mapping | the compute descriptor exists and is not a copy of eth | `get_target("kr260-compute-chiplet").to_host(0x40030000)` | none | `== window_base + 0x4003_0000`; `window_base` from config, **not** hard-coded `0x4_0000_0000` | none | yes | BLOCKED-G-WIN |
| `L0-ADDR-03` | out-of-window fails loud | the wedge class is unreachable from code | `to_host(0x84030000)`, `to_host(-1)`, `to_host(1<<40)` | none | each raises `AddressGuardError`; no `/dev/mem` touched | none | yes | PLANNED |
| `L0-ADDR-04` | peer classification | `is_peer()` keys on the per-target aperture byte | `eth.is_peer(0x2F001000)`, `compute.is_peer(0x41001000)` | none | both `True`; `eth.is_peer(0x41001000) is False` | none | yes | PLANNED |
| `L0-ADDR-05` | eth literals rejected on compute | `0x2E03_xxxx` on compute is `mgr_remap_0`, not TideLink | assert the compute descriptor's tlapb base ≠ `0x2E03_0000` | none | compute tlapb base is its own derived value; poking `0x2E03_0000` at a compute board is refused | none | yes | PLANNED |
| `L0-ADDR-06` | window bounds off-by-one | edge correctness of the guard | `to_host(window_size-4)` ok; `to_host(window_size)` raises | none | as stated | none | yes | PLANNED |
| `L0-ADDR-07` | unaligned / non-word addresses | the guard rejects what `mmap` cannot serve | `to_host(0x2E032109)` | none | rejected or explicitly documented as allowed | none | yes | PLANNED |
| `L0-REGS-01` | lane-status decode, die_a | bit positions of the link-up criterion | `decode_lane_status(0x05890000)` — the real silicon value | none | `.fcsm==4`, `.cal_done==1`, `.cr_seen==1`, `.crack_seen==1` | none | yes | PLANNED |
| `L0-REGS-02` | lane-status decode, die_b | same, the flip value | `decode_lane_status(0x27890000)` | none | `.fcsm==4`, `.cal_done==1` | none | yes | PLANNED |
| `L0-REGS-03` | CAM rule encoding | `[0]=en [15:8]=match [23:16]=replace` | `cam_rule(0x2F,0x2D)`, `cam_rule(0x2F,0x23)`, `cam_rule(0x2F,0x2A)`, `cam_rule(0x41,0x2D)` | none | `0x002D2F01`, `0x00232F01`, `0x002A2F01`, `0x002D4101` | none | yes | PLANNED |
| `L0-REGS-04` | role inversion | `role_status[0]` is `role_effective`, **0 = master** | `decode_role(0x02)` | none | reports master + locked, not slave | none | yes | PLANNED |
| `L0-REGS-05` | `ROLE_CFG` ≠ `ROLE_STATUS` | the `0x2080`/`0x2084` trap | assert `ROLE_CFG==0x2080`, `ROLE_STATUS==0x2084` | none | as stated — writing `0x2084` never locks the role | none | yes | PLANNED |
| `L0-REGS-06` | sticky-fault mask | `STATUS[3:1]` = OVERRUN / UNDERRUN / MASTER_ERROR | `decode_status(0xE)` | none | all three flagged; bit 0 ignored | none | yes | PLANNED |
| `L0-REGS-07` | `FCSM_LINK_IDLE` is 4 | the one magic number | `assert FCSM_LINK_IDLE == 4` | none | as stated | none | yes | PLANNED |
| `L0-TGT-01` | registry completeness | both designs are registered | `TARGETS.keys()` | none | contains `kr260-eth-chiplet` and `kr260-compute-chiplet` | none | yes | PLANNED |
| `L0-TGT-02` | unknown target fails loud | no silent default | `get_target("nope")` | none | `KeyError` | none | yes | PLANNED |
| `L0-TGT-03` | **inbound targets differ per die** | the single most important asymmetry | compare `.inbound_targets` | none | eth `{sram:0x2D, mailbox:0x23}`; compute `{sram:0x2D, mailbox:0x2A}` | none | yes | PLANNED |
| `L0-TGT-04` | peer apertures differ per die | eth `0x2F`, compute `0x41` | compare `.peer_aperture` | none | `0x2F` vs `0x41`; the compute value is flagged `[DERIVED]` in the descriptor | none | yes | BLOCKED-G-ADDR |
| `L0-SAFE-01` | timeout → `WedgeDetected` | a hang never blocks forever | `@guarded(0.1)` around a 1 s sleep | none | `WedgeDetected` within ~0.1 s | none | yes | PLANNED |
| `L0-SAFE-02` | `require_link_up` gates peer access | a peer access on a down link hangs the PS bus | fake board with `link_up()==False` | none | `LinkDownError` | none | yes | PLANNED |
| `L0-SAFE-03` | peer write refused when link down | the guard is on the write path, not advisory | `pair.peer_write()` on a down link | none | `LinkDownError` and **no** write issued | none | yes | PLANNED |
| `L0-SAFE-04` | imports with no hardware | L0 stays runnable in CI | `import hetsoc` in a venv with no `pynq`, no `/dev/mem` | none | import succeeds; hardware access is lazily imported | none | yes | PLANNED |
| `L0-CFG-01` | config precedence | `$HETSOC_CONFIG` → `./hetsoc.toml` → `~/.config/hetsoc.toml` | three fixture files | none | first found wins | none | yes | PLANNED |

---

## L0-SIM — pre-silicon het-pair simulation  ·  no board  ·  CI: nightly (long)  ·  18 rows

Owned by **Sim** (`sim/**`, `docs/SIM_PLAN.md`); listed here so the matrix is
complete and so the Sim owner has a target id set. All are `BLOCKED-G-TB` until the
het testbench exists — see [`ARCHITECTURE.md` §8](ARCHITECTURE.md) for what building
it costs (it is real editing, not a parameter flip).

| ID | Name | Proves | Method | Prereq | Pass criteria | Wedge | CI | Status |
|---|---|---|---|---|---|---|---|---|
| `L0-SIM-01` | het pair elaborates | two **different** tops instantiate and connect | VCS elab of `tb_het_pair.sv`: eth top + compute top, pads crossed via `pad_skid` with `pad_en` gating, I²C wired-AND | both repos' flists merged | 0 elaboration errors | none | yes | BLOCKED-G-TB |
| `L0-SIM-02` | het link bring-up | the proven recipe works across two designs | **per-die** `TLAPB_BASE`; eth `ROLE_CFG(0x2E032080)=0x02`, compute `ROLE_CFG(0x40032080)=0x03` `[DERIVED]`; poll `cal_done`; `SWI_TRAINING_MODE=0`; `WL_LINK_ENABLE_RESET` ← `0x00027F08`, `0x00027F00`, `0x00027F07` | `L0-SIM-01` | both dies `fcsm==4`, `cal_done==1`; die B `cr_seen & crack_seen` | none | yes | BLOCKED-G-TB |
| `L0-SIM-03` | eth → compute SRAM | forward data plane, het | eth CAM `0x2F→0x2D`; write `0x2F001000` = `0xC0FFEE01` | `L0-SIM-02` | compute `shared_sram_0[0x2D001000] == 0xC0FFEE01`; inbound `haddr[31:24]==0x2D` | none | yes | BLOCKED-G-TB |
| `L0-SIM-04` | compute → eth SRAM | **slave→master direction** — never tested in any sim | compute CAM `0x41→0x2D` `[DERIVED]`; write `0x41001000` | `L0-SIM-02` | eth `shared_sram_0[0x2D001000]` matches | none | yes | BLOCKED-G-TB |
| `L0-SIM-05` | eth → compute mailbox | the **`0x2A`** replace byte, not `0x23` | eth CAM `0x2F→0x2A`; 4 words + `SLOT0_CTRL=MSG_VALID` | `L0-SIM-02` | compute `ipc_mailbox_0` `0x2A00_0000..+0x0C` + `MSG_VALID` | none | yes | BLOCKED-G-TB |
| `L0-SIM-06` | compute → eth mailbox | the `0x23` replace byte, reverse | compute CAM `0x41→0x23` | `L0-SIM-02` | eth `ipc_mailbox_0` `0x2300_0000..+0x0C` + `MSG_VALID` | none | yes | BLOCKED-G-TB |
| `L0-SIM-07` | CAM-off identity control | `0x2D` really came from the CAM | `CAM_CTRL=0`; repeat `L0-SIM-03` at a distinct offset | `L0-SIM-03` | far die sees the untranslated upper byte (`0x2F` / `0x41`) | none | yes | BLOCKED-G-TB |
| `L0-SIM-08` | inbound confinement DECERR | the security boundary, both directions | CAM replace = an **excluded** byte (`0x2C`, `0x2A` on the eth die, `0x21`) | `L0-SIM-02` | far-die matrix DECERRs; no wedge; excluded region unchanged | none | yes | BLOCKED-G-TB |
| `L0-SIM-09` | read round-trip both ways | the fragile read-return path | read back `0x2F001000` / `0x41001000` over the link | `L0-SIM-03/04` | payload returns, `hresp==0` | none | yes | BLOCKED-G-TB |
| `L0-SIM-10` | multi-word burst | cross-beat off-by-one one beat cannot catch | 8 words `+0x1000..0x101C`, values `0x5EED0000+(i<<4)+i` | `L0-SIM-03` | all 8 land in order | none | yes | BLOCKED-G-TB |
| `L0-SIM-11` | mailbox IRQ source latches | the cross-die interrupt *source* | after `L0-SIM-05`, read far-die mailbox `+0x028` | `L0-SIM-05` | `IRQ_STATUS[0]==1` | none | yes | BLOCKED-G-TB |
| `L0-SIM-12` | `d2d_irq` → NVIC wiring | the two dies' NVIC maps differ as documented | drive each source; probe both vectors | `L0-SIM-01` | eth `[7:0]`→CPU0 IRQ[17:10], `[15:8]`→CPU1 IRQ[16:9]; compute `[7:0]`→**M4** NVIC[1..8], `[15:8]`→**M0+** NVIC[13..20] | none | yes | BLOCKED-G-TB |
| `L0-SIM-13` | TX-aperture wedge gate | link-down TX access faults instead of hanging | port `verif/chiplet_d2d_decode/tb_tx_gate.sv` to **both** window bases | none | `hsel_tx==0` when `link_active=0`; clean 2-cycle AHB ERROR; `hsel_tlapb` **still** selectable (bring-up must work with the link down) | none | yes | PROVEN-SIM (`verif/chiplet_d2d_decode`, eth window only) |
| `L0-SIM-14` | HREADY-loop guard | the decoder's `dph_peer` loop break, on both window shapes | port `tb_hready_loop.sv`; 4 back-to-back NONSEQ peer writes, no IDLE beats | none | 4 writes land, `write_count==4` | none | yes | PROVEN-SIM (`verif/chiplet_d2d_decode`, eth window only) |
| `L0-SIM-15` | **compute decode aliasing + peer byte** | resolves `0x40` vs `0x41`, and the 240 MB alias | instantiate the **real** `nanosoc_compute_chiplet` top (not the decoder-bypassing `tb_soc_pair.sv`); sweep `0x40..0x4F` | `L0-SIM-01` | peer aperture answers at `0x41` and every odd 16 MB slot; config at every even slot; the compute descriptor matches | none | yes | BLOCKED-G-ADDR |
| `L0-SIM-16` | TideChart election over a real link | election has **never** been simulated over TideLink | het pair, both dies `TC_CTRL[0]`, widened `TC_TIMEOUT` | `L0-SIM-02` | exactly one `is_root`; identical `TC_BEST_CLAIM` both sides | none | yes | BLOCKED-G-TC |
| `L0-SIM-17` | asymmetric reset ordering | the far-die-dark hazard | hold die B in reset, bring die A up, release B; repeat with the order swapped, ×N | `L0-SIM-01` | link converges either way; no false-FULL wedge. ⚠ an idealised sim resolves demets cleanly and **may pass vacuously** — see `RISK-6` | none | yes | BLOCKED-G-TB |
| `L0-SIM-18` | error injection / recovery | whether a bit error is recoverable or terminal | reuse `tidelink/cocotb/tidelink_error_injection` against the het pair; inject ACK-loss / pktnum gap on an AXI FC node | `L0-SIM-02` | with recovery restored the link recovers. **Today it is expected to wedge** — this is the regression that gates `G-WEDGE` | none | yes | BLOCKED-G-WEDGE |

---

## L1 — one board, read-only probes  ·  wedge: none  ·  CI: nightly  ·  17 rows

`tests/test_l1_{probe,link,health,tidechart}.py`

| ID | Name | Proves | Method | Prereq | Pass criteria | Wedge | CI | Status |
|---|---|---|---|---|---|---|---|---|
| `L1-PROBE-01` | eth boot-ROM aliveness | the PS→SoC backdoor delivers into a live SoC | read `0x4_0000_0000 + 0x00..0x0C` | eth bitstream loaded | `0x18003C00, 0x08000189, 0x080001CD, 0x080001CF` | none | yes | PROVEN-HOM |
| `L1-PROBE-02` | compute boot-ROM aliveness | same, compute die | read `window_base + 0x00..0x0C` | compute bitstream | expected words `[TBD]` — must come from the compute build's bootrom image | none | yes | BLOCKED-G-FPGA |
| `L1-PROBE-03` | board reachable | ssh + sudo + `/dev/mem` all work | `Board.alive()` | lease held | `True` within the timeout | none | yes | PROVEN-HOM |
| `L1-PROBE-04` | bitstream loaded | `fpga_manager` reports `operating` | read sysfs after deploy | deploy done | `operating`; AFI PS-master widths read 32-bit on both ports | none | yes | PROVEN-HOM |
| `L1-PROBE-05` | no bare-link address is ever issued | the wedge class is unreachable in practice | assert nothing opens `0x8403_xxxx` / `0xA400_xxxx` / `0x8000_0000`; audit the staged scripts | none | no bare-link tool (`kr260_smoke.py`, `tl39.py`, `kr260_credit_tx.py`, `kr260_drain.py`, `kr260_onchip_*.py`) is staged on a chiplet board | none | yes | PLANNED |
| `L1-PROBE-06` | AFI canary suppression | the deploy path does not re-wedge on load | assert `KR260_AFI_NO_CANARY=1` for chiplet targets | deploy flow | canaries skipped, width fix still runs | none | yes | PROVEN-HOM |
| `L1-LINK-01` | lane status readable with the link **down** | the config plane is reachable exactly when needed | read `SWI_LANE_STATUS` (eth `0x4_2E03_2108`) | bitstream | decodable value returned; `a_tlapb` carries no `tx_open` term so it cannot be gated off | none | yes | PROVEN-HOM |
| `L1-LINK-02` | role status readable | the per-die strap is observable pre-bring-up | read `ROLE_STATUS` (eth `0x4_2E03_2084`) | bitstream | eth `effective_role=0` (master); compute `=1` (slave) | none | yes | PROVEN-HOM (eth pair: die_a=0, die_b=1) |
| `L1-LINK-03` | Wlink lane activity | the only bits **not** downstream of role-lock | read `0x4_2E03_0234`: [3] tx_active, [4] rx_valid | bitstream | both readable — the most informative bits when a link will not come up | none | yes | PLANNED |
| `L1-LINK-04` | `link_active` ≡ `role_locked` | do not treat `link_active` as independent | read `ROLE_STATUS[1]`; compare with any `link_active` mirror | bitstream | identical — `assign link_active = role_locked_o` | none | yes | PLANNED |
| `L1-HEALTH-01` | sticky faults | baseline for every data-plane test | read `STATUS 0x4_2E03_2010`, mask `[3:1]` | bitstream | readable; baseline recorded | none | yes | PROVEN-HOM |
| `L1-HEALTH-02` | credit count | sideband FIFO occupancy | read `CREDIT_COUNT 0x4_2E03_200C [12:0]` | bitstream | readable. ⚠ `4096` means **idle**, not broken | none | yes | PROVEN-HOM (steady 4096) |
| `L1-HEALTH-03` | far-end credit observation | the sideband far-end view | read `OBS_FC_CREDIT 0x4_2E03_219C` | bitstream | readable | none | yes | PROVEN-HOM (`0xFC00001F`) |
| `L1-HEALTH-04` | **per-node Wlink FC health** | the only visibility into the nodes that actually wedge | read `TLAPB + {AW 0x1000, W 0x1100, B 0x1200, AR 0x1300, R 0x1400, GenBus 0x1600, TideLink 0x1700} + {0x08 tx-fifo, 0x10 ack/nack, 0x20 CRC}` | bitstream | all 21 registers readable; CRC counts and Ack/Nack flags recorded as a baseline | none | yes | PROVEN-HOM (`xfer --mode fc_health`) |
| `L1-HEALTH-05` | sync-detect / lane fault | eye-drift indicator | read `SYNC_DET 0x4_2E03_2114 [31:16]` and `SWI_LANE_STATUS[15:8]` | bitstream | readable; `lane_fault == 0x00` | none | yes | PLANNED |
| `L1-TC-01` | TideChart register plane | the identity block answers | read `0x4_2E04_0000 + {0x00,0x10,0x24,0x2C}` | bitstream | eth: `DEVICE_CLASS=0x0001`, `PORT_COUNT=1`, `local_id=0x1F`, `is_root=0`, `TC_ERROR=0` | none | yes | PROVEN-HOM |
| `L1-TC-02` | port count is per-die | eth has 1 port, compute has 2 | read `TC_PORT_COUNT 0x24` on each | both bitstreams | eth `1`, compute `2` | none | yes | BLOCKED-G-FPGA |

---

## L2 — one board, config-plane writes  ·  wedge: low  ·  CI: nightly  ·  16 rows

`tests/test_l2_{role,cam,link,tidechart}.py`

| ID | Name | Proves | Method | Prereq | Pass criteria | Wedge | CI | Status |
|---|---|---|---|---|---|---|---|---|
| `L2-ROLE-01` | role lock takes | the config plane accepts writes on silicon | write `ROLE_CFG 0x4_2E03_2080` = `0x02` (eth) / `0x03` (compute); read `ROLE_STATUS` | fresh die | `ROLE_STATUS` `0x00 → 0x02`; `effective_role` matches the strap | low | yes | PROVEN-HOM |
| `L2-ROLE-02` | FCSM responds to role lock | the link state machine is live | after `L2-ROLE-01` read `SWI_LANE_STATUS[19:17]` | `L2-ROLE-01` | FCSM advances `0→1` with no peer; `cal_done` stays 0 (expected) | low | yes | PROVEN-HOM |
| `L2-ROLE-03` | role lock is POR-only sticky | a warm reset does not re-open `ROLE_CFG` | write `ROLE_CFG` again with the other role; read back | `L2-ROLE-01` | second write ignored while `role_locked`; only `poresetn` clears it | low | yes | PLANNED |
| `L2-CAM-01` | CAM rule programs + reads back | the translator register file works | write `CAM_BASE 0x4000`=0, `RULE_0 0x4010`=`0x002D2F01`, `CTRL 0x4004`=1; read back | bitstream | all three read back; `RULE_0` decodes to match `0x2F` / replace `0x2D` / enable | low | yes | PROVEN-HOM |
| `L2-CAM-02` | arming order | a half-configured rule is never live | assert the framework writes `BASE → RULE → CTRL`, CTRL last | none | order enforced inside `ChipletPair.program_cam()`; a reordering test is rejected | low | yes | PLANNED |
| `L2-CAM-03` | global enable toggles | CAM off ⇒ identity translation | `CTRL=0` read back; `CTRL=1` read back | `L2-CAM-01` | bit[0] follows | low | yes | PROVEN-HOM |
| `L2-CAM-04` | **CAM does not survive a warm reset** | the documented `hresetn` trap | program the CAM; assert `hresetn` only (not POR); read `CTRL`/`RULE_0` | ability to pulse `hresetn` alone on KR260 `[TBD]` | CAM reads back cleared while `ROLE_CFG`/`role_lock` survive — **the first peer write after this would silently DECERR** | low | yes | PLANNED |
| `L2-CAM-05` | compute CAM base is the derived one | resolves `0x2E034000` vs `0x40034000` | program the CAM at the compute descriptor's base; read back | compute bitstream | reads back; a write at `0x2E034000` would hit `mgr_remap_0` and must be refused by the descriptor | low | yes | BLOCKED-G-ADDR |
| `L2-CAM-06` | 8 rules, rule 0 highest priority | the rule file behaves as documented | program conflicting rules 0 and 1; read back | `L2-CAM-01` | both stored; priority asserted behaviourally at L4 | low | yes | PLANNED |
| `L2-LINK-01` | TX-aperture gate on silicon | a link-down TX access faults, does not hang | with the link down, write `0x4_2E00_0004` | link down | clean bus error (or a framework refusal); **board still alive afterwards** | low | yes | PLANNED |
| `L2-LINK-02` | SW link reset bit | the real software link reset exists | read-modify-write `0x4_2E03_0208` bit[3] on a **down** link | link down (never live — see `L3-LINK-08`) | write accepted; FCSM returns to IDLE | low | yes | PLANNED |
| `L2-LINK-03` | `PAIR_BASE_ADDR` programmable | prerequisite for the returner/doorbell path | write `0x4_2E03_2000` = the peer's TideLink APB base; read back | bitstream | reads back | low | yes | PLANNED |
| `L2-LINK-04` | perf block enable | telemetry is reachable at the post-fix addresses | read `PERF_ID 0x4_2E03_20FC`; if `0x5046_0100`, set `PERF_CTRL 0x20A0` bit[0]; read `PERF_CONG_STATE 0x20F8` | bitstream | `PERF_ID` matches; EWMA advances only after enable (reading 0 before is **correct**, not a bug) | low | yes | PLANNED |
| `L2-TC-01` | widen election timeout | the 256-cycle default is shorter than the D2D round trip | write `TC_TIMEOUT 0x4_2E04_000C` = `0x4000_8000`; read back | bitstream | reads back | low | yes | PROVEN-HOM |
| `L2-TC-02` | TideChart reset semantics | records a known RTL deviation | write `TC_CTRL 0x08` = `0x8`; read `TC_STATUS` | after an election | **`election_done` stays 1** — the test asserts the *observed* behaviour and links the gap | low | yes | PROVEN-HOM (deviation confirmed) |
| `L2-TC-03` | `TC_DEVICE_CLASS` writability | resolves the RO/RW contradiction between the two repos | write `TC_DEVICE_CLASS 0x10` = `0x0002` on the compute die; read back | compute bitstream | either reads back `0x0002` (**RW** — deterministic root with no rebuild) or stays `0x0001` (**RO** — rebuild required). **Record which.** | low | yes | BLOCKED-G-FPGA |

---

## L3 — two boards, link bring-up + control plane  ·  wedge: low  ·  CI: nightly  ·  24 rows

`tests/test_l3_{link,tidechart,irq,health}.py`

| ID | Name | Proves | Method | Prereq | Pass criteria | Wedge | CI | Status |
|---|---|---|---|---|---|---|---|---|
| `L3-LINK-01` | **het link comes up** | ★ the headline result — `M-H1` | concurrent bring-up on both boards: role lock → poll `cal_done` → `SWI_TRAINING_MODE=0` → `WL_LINK_ENABLE_RESET` ← `0x00027F08`, `0x00027F00`, `0x00027F07` → poll FCSM | both bitstreams, ribbon seated, **fresh** dies | **both dies `fcsm==4` and `cal_done==1`** | low | yes | BLOCKED-G-FPGA (`PROVEN-HOM` for eth↔eth, 2026-07-27) |
| `L3-LINK-02` | calibration completes bilaterally | the forwarded-clock winscan trains against a **different design** | read `SWI_LANE_STATUS[16]` on both | `L3-LINK-01` | `cal_done==1` on both within the timeout | low | yes | BLOCKED-G-FPGA |
| `L3-LINK-03` | CR/CRACK exchange seen | FCSM sticky evidence, not just a state number | read `SWI_LANE_STATUS[23]`, `[24]` | `L3-LINK-01` | `cr_seen==1 && crack_seen==1` on both dies | low | yes | BLOCKED-G-FPGA (eth pair: `0x05890000` / `0x27890000`) |
| `L3-LINK-04` | link holds | not a one-cycle transient | read-only re-check ≥30 s after `L3-LINK-01` | `L3-LINK-01` | FCSM still 4, `cal_done` still 1, `lane_fault==0` | low | yes | BLOCKED-G-FPGA |
| `L3-LINK-05` | role asymmetry on silicon | the two dies resolve to opposite roles | read `ROLE_STATUS[0]` on both | `L3-LINK-01` | eth `0` (master), compute `1` (slave) | low | yes | BLOCKED-G-FPGA |
| `L3-LINK-06` | lane-lock is **not** the gate | prevents a false-negative bring-up script | read `SWI_LANE_STATUS[7:0]` after `S_DONE` | `L3-LINK-01` | reads `0x00` and the test **still passes** — the gate is `cal_done`, never `lanes_locked==0xFF` | low | yes | PROVEN-HOM |
| `L3-LINK-07` | bring-up is repeatable | deterministic re-convergence from cold | POR both → deploy → bring up, ×N (N≥5) | fpgahub per-target POR | FCSM=4 + `cal_done` every cycle | low | yes | BLOCKED-G-FPGA |
| `L3-LINK-08` | **never re-bring-up a live link** | a known board-killer, encoded as a guard | attempt bring-up while FCSM==4 | link up | the framework **refuses**; no `LL_SWRESET` is issued. (Doing it desyncs the link and hangs the sender's peer writes — this wedged die_a on 2026-07-29) | low → HIGH if unguarded | yes | PLANNED |
| `L3-LINK-09` | **never role-lock on a dead RX clock** | the false-FULL wedge class (`RISK-6`) | attempt bring-up with the peer board powered down / ribbon removed | one board only | the framework refuses, or gates the `ROLE_CFG` W1S on an RX-clock-present indication (`clkfreq_check`) | low | yes | PLANNED |
| `L3-LINK-10` | PHY version match | a V1/V2 het pair is untested | read the build manifest / PHY identity from both bitstreams | both bitstreams | both report V2 (`TIDELINK_PHY_V2`). A mismatch **fails**, it does not warn | none | yes | BLOCKED-G-FPGA |
| `L3-LINK-11` | lane count match | `NUM_PHY_LANES` must match across the ribbon | compare both designs' parameters | both bitstreams | both `8` | none | yes | PLANNED |
| `L3-LINK-12` | ribbon integrity pre-check | a wiring fault is diagnosed before it looks like an RTL bug | with the link down, read `0x4_2E03_0234` [4] rx_valid on both while the peer drives training | both bitstreams | `rx_valid` toggles on both. A dead `rx_valid` points at the ribbon/pinout, not the SoC | none | yes | PLANNED |
| `L3-CAL-01` | one-shot calibration is observable | `calibrated_once_q` freezes the sampling point | re-read `cal_done` and `SYNC_DET` over ≥10 min | `L3-LINK-01` | `cal_done` never re-asserts; sync-detect / CRC drift recorded — the input to `RISK-2` | low | yes | BLOCKED-G-FPGA |
| `L3-CAL-02` | forced re-cal | `SWI_FORCE_RECAL` is the only re-cal path | write `SWI_FORCE_RECAL` (W1P) `[TBD offset — take from the tidelink REGISTER_MAP]`; observe `cal_done` | `L3-LINK-01` | calibration re-runs; the link returns to FCSM=4 | **HIGH** | no | PLANNED |
| `L3-TC-01` | TideChart plane alive on both dies | prerequisite for any fabric test | read `TC_STATUS` / `TC_DEVICE_CLASS` / `TC_PORT_COUNT` on both | `L3-LINK-01` | eth `PORT_COUNT=1`, compute `=2`; both `DEVICE_CLASS=0x0001` unless `L2-TC-03` changed it | low | yes | BLOCKED-G-FPGA |
| `L3-TC-02` | root election converges | exactly one root in a het pair | widen `TC_TIMEOUT`; write `TC_CTRL=0x1` on **both, as simultaneously as possible** | `L3-TC-01` | **exactly one** `is_root==1`; both `election_done==1`; identical `TC_BEST_CLAIM` | low | yes | **FAILED-HOM** — dual-root observed (both `is_root=1`, each `BEST_CLAIM` = its own); BLOCKED-G-TC |
| `L3-TC-03` | tie-break observability | diagnoses a dual-root when it happens | read `TC_DEVICE_CLASS` + `TC_RANDOM_ID` on both | `L3-TC-02` | classes equal; random_ids **differ**; the winner has the lower id. Equal ids ⇒ collision | low | yes | PROVEN-HOM (die_a `0x57A2`, die_b `0xAA98`) |
| `L3-TC-04` | deterministic root | choose the grandmaster on purpose | set the compute die's `TC_DEVICE_CLASS` lower than eth's (via `L2-TC-03` if RW, else a rebuild), then elect | `L2-TC-03` | compute wins **every** time over ≥5 rounds | low | yes | BLOCKED-G-TC |
| `L3-TC-05` | enumeration | the root walks the tree and assigns IDs | on the root only, `TC_CTRL=0x2`; poll both | `L3-TC-02` | root `{local_id=0, total=2, enum_done, ACTIVE}`; leaf `{local_id=1, enum_done, ACTIVE}` | low | yes | BLOCKED-G-TC |
| `L3-TC-06` | route table | routing programmed by enumeration | write `TC_ROUTE_RD 0x18` = peer id; read back | `L3-TC-05` | root→dest1 egress0 hop1; leaf→dest0 egress0 (uplink) hop1 | low | yes | BLOCKED-G-TC |
| `L3-TC-07` | telemetry broadcast | the link-state agent crosses the sideband | `TC_CONG_CTRL 0x74` = `0x1` then `0x3`; read the peer's `TC_CONG_STATUS 0x78` / `TC_COST_RD 0x7C` | `L3-TC-05` | peer `rx_bcast` ↑, local `tx_bcast` ↑ | low | yes | BLOCKED-G-TC |
| `L3-TC-08` | fabric negatives | enum-on-leaf is a no-op; pre-enum telemetry is dropped | `TC_CTRL=0x2` on the leaf; enable broadcast before enumeration | `L3-TC-05` | leaf neither self-enumerates nor becomes root; pre-enum broadcast count stays 0 (SRC_ID `0x1F` dropped) | low | yes | BLOCKED-G-TC |
| `L3-IRQ-01` | doorbell over the **returner** | the second cross-die master, never validated on silicon | eth writes `DOORBELL 0x4_2E03_2014`; the peer reads `DOORBELL_RESPONSE_ACC 0x4_..._2024` | `L3-LINK-01`, `L2-LINK-03` | the peer's accumulator is non-zero. ⚠ the payload is the free-credit count, so **0 credits ⇒ no IRQ** | low | yes | PLANNED |
| `L3-HEALTH-01` | pair health snapshot | one call, both dies, read-only | `ChipletPair.health()` on both | `L3-LINK-01` | returns credits, sticky faults, per-node CRC and FCSM for both dies; no write issued | none | yes | PLANNED |

---

## L4 — two boards, cross-die DATA PLANE  ·  wedge: **HIGH**  ·  CI: **never**  ·  28 rows

> 🔴 **Every row in this table can wedge both boards.** The cross-die data plane
> intermittently hangs the PS AXI bus with no software timeout — root cause
> `G-WEDGE`. Attended only, behind an explicit flag, with the per-target JTAG-POR
> recovery staged before you start. See
> [`VERIFICATION_PLAN.md` §7 `RISK-1`](VERIFICATION_PLAN.md).

`tests/test_l4_{sram,mbox,confine,irq,dma,ptp,debug,eth}.py`

| ID | Name | Proves | Method | Prereq | Pass criteria | Wedge | CI | Status |
|---|---|---|---|---|---|---|---|---|
| `L4-SRAM-01` | eth → compute SRAM | ★ forward data plane on het silicon — `M-H2` | eth CAM `0x2F→0x2D`; write `0x4_2F00_1000` = `0xC0FFEE01`; compute **local** read of `0x2D00_1000` | `L3-LINK-01` | compute reads `0xC0FFEE01`. The verdict is the **far-die local read** (no link traversal) — the wedge-safe proof | **HIGH** | no | BLOCKED-G-FPGA (`PROVEN-HOM`) |
| `L4-SRAM-02` | compute → eth SRAM | the slave→master direction on het silicon | compute CAM `0x41→0x2D`; write peer `0x41001000`; eth local read of `0x2D00_1000` | `L3-LINK-01` | eth reads the payload | **HIGH** | no | BLOCKED-G-FPGA (`PROVEN-HOM`, payload `0xB2A0FEED`) |
| `L4-SRAM-03` | eth read-back over the link | the read round trip, forward | eth reads `0x4_2F00_1000` | `L4-SRAM-01` | the payload returns | **HIGH** (worst) | no | **FLAKY-HOM** — wedged both boards 2026-07-29 |
| `L4-SRAM-04` | compute read-back over the link | the read round trip, reverse | compute reads `0x41001000` | `L4-SRAM-02` | the payload returns | **HIGH** (worst) | no | BLOCKED-G-FPGA |
| `L4-SRAM-05` | multi-word burst | cross-beat ordering on silicon | 8 words `0x2F001000..0x2F00101C` | `L4-SRAM-01` | all 8 land in order (far-die local read) | **HIGH** | no | BLOCKED-G-FPGA |
| `L4-SRAM-06` | CAM-off control | the translation is what moved the address | `CAM_CTRL=0`; peer-write a distinct offset | `L4-SRAM-01` | the far die sees the **untranslated** upper byte; nothing lands in `0x2D` | **HIGH** | no | PROVEN-SIM (`g2_soc_pair` stage 3) |
| `L4-SRAM-07` | rule priority | rule 0 wins over a conflicting rule 1 | program `RULE_0 = 0x2F→0x2D` and `RULE_1 = 0x2F→0x23`; peer-write | `L2-CAM-06` | the payload lands in `0x2D`, not the mailbox | **HIGH** | no | PLANNED |
| `L4-MBOX-01` | eth → compute mailbox | **the `0x2A` replace byte** — the het-specific case | eth CAM `0x2F→0x2A`; 4 words at peer `+0x00..0x0C`, then `+0x020 = MSG_VALID`; compute local read of `0x2A00_0000` | `L3-LINK-01` | 4 words match, `SLOT0_CTRL[0]==1` | **HIGH** | no | BLOCKED-G-FPGA (eth↔eth `PROVEN-HOM` at `0x23`) |
| `L4-MBOX-02` | compute → eth mailbox | the `0x23` replace byte, reverse | compute CAM `0x41→0x23`; same sequence; eth local read of `0x2300_0000` | `L3-LINK-01` | 4 words match, `MSG_VALID` set | **HIGH** | no | BLOCKED-G-FPGA |
| `L4-MBOX-03` | doorbell IRQ **source** latches | the general-purpose cross-die interrupt, firmware-free | after `L4-MBOX-01`, the receiver reads mailbox `+0x028` | `L4-MBOX-01` | `IRQ_STATUS[0]==1` — a far-die write raised the near-die interrupt source | **HIGH** | no | BLOCKED-G-FPGA (`PROVEN-HOM`) |
| `L4-MBOX-04` | ACK handshake | the return half of the IPC protocol | the receiver sets `SLOT0_CTRL[1]` (ACK); the sender polls it over the link | `L4-MBOX-01` | the sender observes ACK; the slot becomes reusable | **HIGH** | no | PLANNED |
| `L4-MBOX-05` | both slots | slot 1 targets the other core's NVIC | repeat `L4-MBOX-01` on slot 1 | `L4-MBOX-01` | slot-1 data + `MSG_VALID`; the slot-1 IRQ source latches | **HIGH** | no | PLANNED |
| `L4-MBOX-06` | mailbox↔SRAM aperture switch | one aperture cannot reach both inbound targets | send an SRAM message, reprogram the CAM, send a mailbox message, without a link teardown | `L4-SRAM-01`, `L4-MBOX-01` | both land correctly; **quiesce before the CAM write** (see `RISK-5`) | **HIGH** | no | PLANNED |
| `L4-CONF-01` | inbound confinement holds | ★ the security boundary on silicon | CAM replace = an **excluded** byte: `0x2C` (ctrl_dbg), `0x2A` (reset_ctrl, on the **eth** die), `0x21` (QSPI) | `L3-LINK-01`, JTAG-POR staged | the far die DECERRs; **board not wedged**; the excluded region is provably unchanged | **HIGH** | no | PLANNED |
| `L4-CONF-02` | debug windows stay closed | CoreSight is DAP-only from the link | CAM `0x2F→0xA0` / `0x2F→0xB0`; read `0xA000_ED00` (CPUID) over the link | `L3-LINK-01` | DECERR, **no** CPUID returned, no wedge. The negative that must hold until `G-SEC` lands | **HIGH** | no | PLANNED |
| `L4-CONF-03` | code space stays closed | no remote write into a CPU's IMEM/DMEM | CAM `0x2F→0x00` (eth_ss_slave) / `0x2F→0x80` (cpu_ss_1_slave) | `L3-LINK-01` | DECERR, no wedge | **HIGH** | no | PLANNED |
| `L4-CONF-04` | the compute mailbox is **not** at `0x23` | the asymmetry is real, not cosmetic | eth CAM `0x2F→0x23` aimed at the **compute** die | `L3-LINK-01` | DECERR (compute has nothing at `0x23`); nothing lands. Catches a descriptor regression | **HIGH** | no | BLOCKED-G-FPGA |
| `L4-CONF-05` | compute `dap_m` has no off-die path | compute deliberately withholds `d2d0/d2d1` from its own DAP | attempt a compute-DAP-originated peer access | SWD probe on the compute board | refused / DECERR | **HIGH** | no | BLOCKED-G-FPGA |
| `L4-IRQ-01` | packet-committed source | far-die data arrival raises a source | after a cross-die transfer read `STATUS 0x4_2E03_2010` bit[4] | `L4-SRAM-01` | the bit sets; level-sensitive, clears on FIFO read | **HIGH** | no | PLANNED |
| `L4-IRQ-02` | PTP FC-word source | `PTP_CTRL[2]` rx_valid latches | send a PTP FC word; read `0x4_2E03_2034` | `L3-LINK-01`, `PTP_CTRL.enable` | bit[2] sets. ⚠ behind a `TIDELINK_PTP` generate guard — confirm it is in the image | **HIGH** | no | PLANNED |
| `L4-IRQ-03` | TideChart event source | an election/enum edge raises `d2d_irq[14]` | run `L3-TC-02`; watch `TC_STATUS` edges and the `TC_HOTPLUG` sticky | `L3-TC-02` | edge observed | **HIGH** | no | BLOCKED-G-TC |
| `L4-IRQ-04` | **full ISR delivery** | an interrupt reaches a far core's handler | SWD-load firmware that sets mailbox `irq_enable` (`+0x02C`) + the NVIC ISER and flags DMEM/LED in the ISR; the far die runs `L4-MBOX-01` | SWD probe, firmware, core released from the boot-gate | the ISR runs on the receiving die. **The NVIC bit differs per die** — eth CPU1 IRQ0 vs the compute M4/M0+ split | **HIGH** | no | BLOCKED-G-FW |
| `L4-DMA-01` | DMA bulk crossing | zero-copy bulk vs a single CPU beat | DMA ch0 src = local SRAM, **dst = the peer aperture** (`0x2F00_xxxx` / `0x41xx_xxxx`), N bytes; poll done; far-die local read | firmware or PS-replicated DMAC APB writes; core off the boot-gate | far-die SRAM == the source block; DMA done, no ERR | **HIGH** | no | BLOCKED-G-FW |
| `L4-DMA-02` | DMA must not target the TX aperture | the no-backpressure wedge | attempt a DMA descriptor with dst = `0x2E00_0000` (TX aperture) | none | the framework **refuses**; if the RTL gate is trusted, a link-down attempt takes a clean 2-cycle AHB ERROR | **HIGH** | no | PLANNED |
| `L4-PTP-01` | cross-die time sync | PHC discipline over the FC sideband | eth (GM) arms `HW_SYNC_CTRL` + interval; the peer's servo src-0 disciplines its PHC; compare PHC captures | `L3-LINK-01`; PHC enabled on both dies | the far PHC's offset to the GM converges and holds over N syncs. ⚠ judge by **offset convergence, not `servo_locked`** — that bit reports the ha1588 servo, not src-0 | **HIGH** | no | BLOCKED-G-PTP (compute's PHC exports no live time; `.phc_seconds`/`.phc_nanoseconds` tied 0 on **both** compute links) |
| `L4-DBG-01` | cross-die SWD halt | one probe debugs the far die (goal G3) | die A CAM `0x2F→0xA0`; write DHCSR `0xA000_EDF0` = `DBGKEY|C_HALT|C_DEBUGEN`; poll `S_HALT`; read CPUID `0xA000_ED00` | `G-SEC` gate + inbound target-list edit + regen; `G-WEDGE` fixed | the far core halts; CPUID reads `0x410CC200` | **HIGH** (poll-heavy ⇒ inherits the wedge) | no | BLOCKED-G-SEC + BLOCKED-G-WEDGE |
| `L4-ETH-01` | ethernet path alive (M2) | the eth-chiplet-specific function | LAN8720 on PMOD1; MDIO bring-up; MAC out of internal loopback | PHY module, eth firmware, hub topology | PHY link LED, then a frame through the MAC | none (die-local) | no | BLOCKED-G-FW |
| `L4-ETH-02` | ethernet frame relayed across the link | the end-to-end chiplet story: network in → compute die | receive a frame into `eth_scratch_rx 0x3000_0000`; DMA/CPU-copy it into the peer aperture; compute local read of `0x2D00_xxxx` | `L4-ETH-01`, `L4-SRAM-01` | the frame bytes land byte-exact on the compute die | **HIGH** | no | BLOCKED-G-FW + BLOCKED-G-FPGA |

---

## L5 — soak, stress, characterisation  ·  wedge: **HIGH**  ·  CI: **never**  ·  13 rows

`tests/test_l5_{soak,perf,recovery,char}.py`

| ID | Name | Proves | Method | Prereq | Pass criteria | Wedge | CI | Status |
|---|---|---|---|---|---|---|---|---|
| `L5-SOAK-01` | write-only soak + health | sustained integrity, wedge-safely | N peer-write beats cycling a 16-word window; sample `SWI_LANE_STATUS`, `STATUS`, `CREDIT_COUNT` every 50 beats | `L4-SRAM-01` | `mismatches==0`, `FCSM_min==4`, `cal` held, `sticky_seen==0x0` | **HIGH** | no | PROVEN-HOM (2000 beats: 0 mismatches, `CREDIT_COUNT` steady 4096, `OBS_FC_CREDIT=0xFC00001F`) |
| `L5-SOAK-02` | soak with per-beat read-back | the fragile read path under load | as above with `--soak-readback` | `L5-SOAK-01` | 100 % readback over N | **HIGH** (worst) | no | FLAKY-HOM |
| `L5-SOAK-03` | bidirectional soak | both dies pushing at once | run `L5-SOAK-01` concurrently on both boards | `L4-SRAM-01/02` | both pass; no sticky fault on either side | **HIGH** | no | PLANNED |
| `L5-SOAK-04` | mailbox soak | doorbell semantics under repetition | N mailbox send / recv / ACK cycles | `L4-MBOX-04` | every message delivered exactly once; no slot corruption | **HIGH** | no | PLANNED |
| `L5-PERF-01` | write throughput | the first het bandwidth number | timed `L5-SOAK-01`, sweeping beat count | `L5-SOAK-01` | bytes/s recorded with variance — a **number**, not a pass/fail | **HIGH** | no | PLANNED |
| `L5-PERF-02` | round-trip latency | the first het latency number | timed single peer read, N samples | `L4-SRAM-03` | median + p99 recorded | **HIGH** | no | PLANNED |
| `L5-PERF-03` | credit occupancy under load | whether the sideband ever throttles | sample `CREDIT_COUNT` / `OBS_FC_CREDIT` / `PERF_CONG_STATE` during `L5-SOAK-03` | `L2-LINK-04` | occupancy range recorded. ⚠ these are **sideband only** — they do not observe the AXI nodes | **HIGH** | no | PLANNED |
| `L5-RECOV-01` | teardown + re-bring-up ×N | deterministic re-convergence | POR both (role_lock clears only on `poresetn`) → deploy → bring up → `L4-SRAM-01`, ×N | fpgahub per-target POR | FCSM=4 + `cal_done` every cycle; the transfer passes after each | **HIGH** | no | PLANNED |
| `L5-RECOV-02` | wedge detect → POR → retry | the framework survives its own hazard | run `L4-SRAM-03` in a loop until a wedge; assert the recovery path | `L4-SRAM-03` | `WedgeDetected` raised (never an infinite block); per-target POR issued **one board at a time, ~8 s apart**, retried once on a transient "cable not found"; both boards return | **HIGH** | no | PARTIAL-HOM (procedure proven manually, not yet automated) |
| `L5-RECOV-03` | health-poll interim mitigation | the no-rebuild workaround for `G-WEDGE` | poll the per-node FC regs (`B 0x1200`, `R 0x1400`; `+0x20` CRC, `+0x10` Ack/Nack) **between** transfers; on a rising CRC or a stuck FIFO, re-cal / FLUSH and retry instead of transacting | `L1-HEALTH-04` | a measurable drop in wedge rate versus the unguarded loop | **HIGH** | no | PLANNED |
| `L5-RECOV-04` | SW link reset recovery | recover a desynced link without a POR | on a **broken** link write `0x4_2E03_0208` bit[3]; re-run bring-up | link already down | the link returns to FCSM=4 without a JTAG POR | **HIGH** | no | PLANNED |
| `L5-CHAR-01` | BER / CRC over time | quantifies the marginal eye | log per-node CRC counts + `SYNC_DET` every minute for ≥1 h under `L5-SOAK-01` | `L1-HEALTH-04` | a CRC-vs-time curve is recorded and correlated with the first wedge | **HIGH** | no | PLANNED |
| `L5-CHAR-02` | deploy repeatability | build/deploy is not the variable | reflash + bring up + `L4-SRAM-01`, ≥10 iterations, JSON output | `L3-LINK-07` | pass rate recorded per stage; every failure attributed to a stage | **HIGH** | no | PARTIAL-HOM (a 3-iteration run: iter 1 wedged at `sram_rtt`, iters 2-3 could not deploy) |

---

## Retired / superseded

*(none yet — ids are never recycled; struck-through rows land here)*

---

## Coverage roll-up — against "all D2D communication and functionality"

| Feature area | Covered by | Deepest status anywhere |
|---|---|---|
| Link bring-up & calibration | `L3-LINK-01..04`, `L3-CAL-01/02`, `L0-SIM-02` | `PROVEN-HOM` |
| Role / strap negotiation | `L2-ROLE-01..03`, `L3-LINK-05`, `L3-LINK-09` | `PROVEN-HOM` |
| FCSM states | `L1-LINK-01`, `L2-ROLE-02`, `L3-LINK-01/03/06` | `PROVEN-HOM` |
| Address translation (CAM) | `L0-REGS-03`, `L2-CAM-01..06`, `L4-SRAM-06/07`, `L0-SIM-07/15` | `PROVEN-HOM` |
| Peer apertures | `L0-ADDR-04`, `L0-TGT-04`, `L4-SRAM-*`, `L0-SIM-15` | `PROVEN-HOM` (eth); `[DERIVED]` (compute) |
| **Inbound confinement / security** | `L4-CONF-01..05`, `L0-SIM-08` | **PLANNED — never tested anywhere, in sim or on silicon** |
| Inbound target 1 — shared SRAM | `L4-SRAM-01/02` | `PROVEN-HOM` |
| Inbound target 2 — IPC mailbox | `L4-MBOX-01..06` | `PROVEN-HOM` at `0x23`; the `0x2A` case untested |
| Direction M→S | `L4-SRAM-01`, `L4-MBOX-01` | `PROVEN-HOM` |
| Direction S→M | `L4-SRAM-02`, `L4-MBOX-02`, `L0-SIM-04` | `PROVEN-HOM` on silicon; **never in any sim** |
| IPC doorbell / mailbox semantics | `L4-MBOX-03..06` | `PROVEN-HOM` (source latch only) |
| Cross-die interrupts → NVIC | `L3-IRQ-01`, `L4-IRQ-01..04`, `L0-SIM-12` | sources `PROVEN-HOM`; **delivery never tested** |
| DMA-driven bulk crossing | `L4-DMA-01/02` | `BLOCKED-G-FW` |
| PTP / PHC over the D2D sideband | `L4-PTP-01`, `L4-IRQ-02` | `BLOCKED-G-PTP` |
| TideChart identity / election / routing | `L1-TC-01/02`, `L2-TC-01..03`, `L3-TC-01..08`, `L0-SIM-16` | register plane `PROVEN-HOM`; election **FAILED-HOM** |
| Flow control & credits | `L1-HEALTH-02..04`, `L5-PERF-03` | `PROVEN-HOM` (sideband only — blind to the AXI nodes) |
| Error injection & recovery | `L0-SIM-18`, `L5-RECOV-02..04` | `BLOCKED-G-WEDGE` |
| Link teardown / re-bring-up | `L3-LINK-07/08`, `L5-RECOV-01/04` | `PLANNED` |
| Reset & power-domain ordering | `L2-CAM-04`, `L2-ROLE-03`, `L3-LINK-09`, `L0-SIM-17` | `PLANNED` |
| Ethernet path (M2) | `L4-ETH-01/02` | `BLOCKED-G-FW` |
| Performance / characterisation | `L5-PERF-*`, `L5-CHAR-*` | `PARTIAL-HOM` |
