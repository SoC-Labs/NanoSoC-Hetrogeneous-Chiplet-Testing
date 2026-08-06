# Resolution — what to change, where, and how to verify it

Everything blocking the heterogeneous bench, as concrete actions. Each has been
located and verified against the pinned trees; none of it is guesswork.

**Nothing here can be done from this repo.** Every item is a write or a push
into another repository, so they are prepared rather than applied — patches in
[`../patches/`](../patches/), commands below.

State when written: het repo `b022761`, eth-chiplet `9d9d107`,
compute-chiplet `c60faca` (both pins live on their remotes).

---

## 1. The critical path — one compute rebuild, two one-line edits

Both blockers live in `NanoSoC-Compute-Chiplet` and clear in a single Vivado
turnaround.

```sh
cd /home/dam1n19/SoCLabs/NanoSoC-Compute-Chiplet
git apply /path/to/het-repo/patches/0001-F7-compute-self-arm.patch
git apply /path/to/het-repo/patches/0002-H6-compute-strap.patch
# then rebuild kr260-compute-chiplet
```

| | Patch | Site |
|---|---|---|
| **F7** the die cannot role-lock | `0001` | `src/rtl/nanosoc_compute_chiplet.sv:698`, `:899` |
| **H6** strap contradicts ball map | `0002` | `tidelink/fpga/targets/kr260-compute-chiplet/tidelink_design.tcl:159` |

**Why the strap and not the pinout.** Non-flip compute transmits on `AC14`,
which is the eth die's *receive* ball — the conductors are already correct for
pairing with eth die_a. Only the role is wrong. Switching to `-flip` to get
strap 1 would put both dies' TX on `AD15` and drive every ribbon conductor from
two outputs; the runbook already forbids that, and `L0-BUILD-05` exists to catch
anyone trying it.

**Naming consequence, decide deliberately.** After `0002`, non-flip compute is
the **die_b** image, inverting the eth convention where non-flip = die_a. Rename
the targets to `-die-a`/`-die-b`, or say it loudly in `BUILD_NOTES.md`.

**Consider bundling** `.AUTO_ANCHOR_EN(1'b1)` into the same rebuild — the eth die
sets it and compute does not, which is `L0-BUILD-02`'s standing finding.

### Verifying, for free, with no board

```sh
cd <het repo> && make test-offline
```

`L0-BUILD-01` and `L0-BUILD-04` are `xfail(strict=True)`. The moment the rebuild
lands they become **failures**, demanding the gates be converted to plain
assertions. The fix cannot land silently, and you learn it worked in 0.2 s
instead of a bench session.

---

## 2. Three pushes — minutes, no rebuild

Each publishes a commit that already exists. Two of them are also **backups**:
the commits survive only as unreferenced objects on one machine and are
gc-eligible.

| # | What | Why it matters | Command |
|---|---|---|---|
| 2a | TideLink `74c6777` | `compute-chiplet c60faca` pins it, and `git ls-remote` finds **zero** refs for it. The freshly-pushed compute chiplet **is not clonable**. | `cd NanoSoC-Compute-Chiplet/tidelink && git push github 74c6777:refs/heads/integ/i1-fix-2026-07-31` |
| 2b | gpio-phy `6ee8418` | GitHub lacks it; GitLab has it as `feat/standalone-phy-bist`. Blocks any fresh recursive clone of the eth chiplet. | from the submodule: `git push origin 6ee8418:refs/heads/feat/standalone-phy-bist` |
| 2c | The OpenOCD DPIDR fix | Uncommitted in `deps/eth-chiplet/nanosoc-multicore-system`; blocks that nested pin from advancing. Substantive — corrects `0x0BB1_1477` (the `DAP_TARGETID`) to the real DPIDR `0x6BA0_2477`, cited to `cxdapswjdp_sw_dp_constants.v:24-25`. | its author commits it |

> 2c is not mine to commit and I have deliberately left it. Discarding it to
> advance a pin would trade a real fix for a version number.

---

## 3. Bench access — one line, unblocks every hardware level

Neither board can be driven from this host: `/dev/mem` needs root and there is
no NOPASSWD sudo and no configured credential. This blocks `kr260_01` as well as
`kr260_02` — it is one problem, not two.

Either:

```sh
echo 'KR260_PASSWORD=...' >> <het repo>/site.local.sh      # read by the Makefile and hetsoc
```

or configure NOPASSWD sudo for `ubuntu` on both boards. **Prefer the latter** —
it is what CI needs, and it avoids a password in a file.

---

## 4. Not ours — hand over

**F6 (TideLink autonegotiation).** `docs/TIDELINK_HANDOVER.md` is written and
unsent. Note its **P2**: the chiplets build V1 while TideLink's autonomy proof is
`TIDELINK_PHY_V2=1`, and their own comment at `axi_chiplet_controller.sv:3236`
says the training-exit fix is *"V2-ONLY … UNSATISFIABLE"* on V1. That may be the
entire answer, in which case the fix is "build V2", not "debug autoneg".

**The eth V1 flist gap.** `local_overrides/Wlink.v` instantiates
`tidelink_winscan_obs` and `tidelink_fcemit_obs`, which are listed only in
`tidelink_fpga_v2.flist` — not in `tidelink_fpga.flist`, which this sim *and the
eth chiplet's own builds* resolve. Backfilled testbench-side in
`sim/het_pair/Makefile` so it self-retires when upstream is corrected. Whether
the right fix is "add to V1" or "guard the instantiation" is a TideLink call.

---

## 5. Order, and what each unlocks

1. **The three pushes** — minutes. Makes every result reproducible off this
   machine and backs up two gc-eligible commits.
2. **The compute rebuild** (patches `0001` + `0002`) — the only thing on the
   critical path. `L0-BUILD-01` tells you the moment it lands.
3. **Board credential** — parallel with 2. Unblocks L1 upward.
4. **Send the handover** — parallel, not ours.

After 1–3 the first heterogeneous bench session becomes worth booking, and the
S0 harness (`hetsoc.jobring` / `anchor` / `kernel` / `bit27`) is already written
and offline-verified against it.
