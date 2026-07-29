# CI, flows and the bench

How the build/test automation fits together: what runs where, what is allowed
to run unattended, and how the two chiplet designs are pinned.

Owned by the Flows area (`Makefile`, `set_env.sh`, `.gitmodules`, `scripts/`,
`flows/`, `ci/`, `.github/`). See [`REPO_LAYOUT.md`](REPO_LAYOUT.md) for the
full ownership map.

---

## 1. The one-minute version

```bash
source set_env.sh
make deps            # submodules + venv + pip install -e host/
make test-offline    # L0 — no boards, always safe
make bench-status    # read-only probe of both boards, cannot wedge
make help            # everything else
```

Nothing above touches a board except `bench-status`, and that only reads.

---

## 2. Test levels and where each one runs

The level convention is defined in [`REPO_LAYOUT.md`](REPO_LAYOUT.md). This is
where each level actually executes:

| Level | Make target | Needs | Runner | Cadence |
|---|---|---|---|---|
| **L0** | `make test-offline` | nothing | GitHub-hosted | every push |
| **L1/L2** | `make test-single` | 1 board | self-hosted (lab) | nightly |
| **L3** | `make test-pair` | 2 boards | self-hosted (lab) | nightly |
| **L4** | `make test-dataplane` | 2 boards | **a human** | attended only |
| **L5** | `make test-soak` | 2 boards | **a human** | attended only |

`make regress` runs L0 → L1/L2 → L3 in that order and stops climbing the moment
a level fails. `make regress --offline` (or `ARGS=--offline`) is the CI subset.

### Marker mapping

`scripts/run_pytest.sh` is the only place a level becomes a pytest selector.
Nothing else should hard-code a marker expression:

| Level | Marker expression | Extra suite flags |
|---|---|---|
| `l0` | `not hardware` | — |
| `l1l2` | `hardware and single_board and not pair and not data_plane and not soak` | — |
| `l3` | `hardware and pair and not data_plane and not soak` | — |
| `l4` | `data_plane` | `--data-plane` |
| `l5` | `soak` | `--data-plane --soak-iters=N` |

The `--data-plane` flag is **not** redundant with the marker. `tests/conftest.py`
deselects every `data_plane`/`soak` test unless it is passed, so selecting the
marker alone collects nothing. That is deliberate: two independent interlocks,
one guarding the operator (`I_ACCEPT_WEDGE_RISK`) and one guarding the suite.

`--allow-peer-read` (the read-back round trip, the most wedge-prone operation in
the suite) is a third interlock and is never enabled by default. Set
`HETSOC_ALLOW_PEER_READ=1` alongside the other two if you want it.

---

## 3. Why CI never runs L4 or L5

The cross-die data plane **intermittently hangs on current silicon**. The
shipped build carries recovery-stripped AXI FCSMs, so a single bit error on the
link has no recovery path: the link stops, and the next PS access to the peer
aperture hangs the ZynqMP AXI bus **with no timeout**. The board drops to 100%
packet loss and comes back only via a JTAG POR issued from another host.

An unattended job cannot issue that POR at 3am. An unattended job that can wedge
the shared bench is not a CI job, it is a way to lose the lab a day. So:

- there is no workflow, target, or `run_ci.sh` mode that runs L4/L5;
- `ci/run_ci.sh` pins `I_ACCEPT_WEDGE_RISK=0` regardless of the environment it
  inherits, so mis-editing a workflow still cannot get there;
- `scripts/run_pytest.sh` enforces the gate itself, so calling the script
  directly is exactly as guarded as going through `make`.

To run them, attended, with a POR to hand:

```bash
I_ACCEPT_WEDGE_RISK=1 make test-dataplane
I_ACCEPT_WEDGE_RISK=1 make test-soak
```

Both print a full-width warning banner first. Read [`SAFETY.md`](SAFETY.md)
before you do this.

When L4 does fail, `scripts/regress.sh` records it **non-gating**: on current
silicon an L4 failure is a characterisation datapoint, not a regression signal.
`ci/results_to_junit.py` renders non-gating failures as `<skipped
type="non-gating">` for the same reason — a dashboard that is red every night
is a dashboard nobody reads.

---

## 4. GitHub Actions

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) has two jobs.

### `offline` — hosted, every push/PR

Lint + L0. Built to depend on as little as possible so it is always green when
the code is right:

- `submodules: false`. The chiplet submodules are declared over **SSH**
  (`git@github.com:…`) and a hosted runner has no key for them. L0 needs no RTL.
- `ci/run_ci.sh offline` therefore runs `make venv`, **not** `make deps`.
- Python 3.11 (stdlib `tomllib`).

### `nightly-hardware` — self-hosted, in the lab

L1/L2 + L3 on the real pair. Runs on schedule (02:30 UTC) or manual dispatch
with `run_hardware: true`.

- `runs-on: [self-hosted, linux, soclabs-bench, kr260-pair]` — the lab machine
  with ssh reach to the boards, an fpgahub client, and the submodule SSH keys.
- `concurrency: kr260-pair-bench`, `cancel-in-progress: false`. One bench, one
  job: two jobs deploying to the same boards is a wedge, not a flaky test.
- leases both boards, releases them in an `always()` step;
- on failure, runs `flows/recover.sh` — POR only, never a reflash.
- `ci/run_ci.sh nightly` runs `make deps` here, because the bench flows shell
  out to TideLink's `kr260_eth_run.sh` / `deploy_pair_role`, which live in the
  submodules.

**The self-hosted runner does not exist yet.** Until it is registered with those
labels the nightly job simply never picks up. That is the intended failure mode:
no runner is better than the wrong runner.

---

## 5. Submodule versioning policy

### What is a submodule, and what is not

```
deps/eth-chiplet          -> NanoSoC-Ethernet-Chiplet     (submodule)
deps/compute-chiplet      -> NanoSoC-Compute-Chiplet      (submodule)
deps/eth-chiplet/tidelink -> TideLink                     (NOT ours — theirs)
```

**TideLink and TideChart are deliberately not direct submodules of this repo.**
They are already submodules of both chiplets, *at different commits*. A third
pin here would produce a checkout that matches neither die — the lab's
"two-checkouts trap", where the tree you read is not the tree that built the
bitstream on the board, and you spend an afternoon debugging line numbers that
do not exist on silicon.

So the D2D IP is reached through the chiplet that owns it. `set_env.sh` sets:

```
TIDELINK_HOME  = deps/eth-chiplet/tidelink
TIDECHART_HOME = deps/eth-chiplet/tidechart
```

The eth-chiplet pin is authoritative because that is the die whose link has
actually been proven on silicon (FCSM=4, 2026-07-27). `flows/deploy_pair.sh`
still uses **each die's own** TideLink for that die's FPGA flow — the compute
side's `fpga/Makefile` is the compute chiplet's, not the eth chiplet's.

### The eth-chiplet pin is on a branch, not on main

`.gitmodules` pins `deps/eth-chiplet` to **`feat/tidelink-chiplet-port`**, not
`main`, and this is load-bearing:

| | TideLink pin | `fpga/targets/kr260-eth-chiplet` |
|---|---|---|
| `main` (`640f408`) | `43c3d7c` | **absent** — only pynq-z2 / mps3 |
| `feat/tidelink-chiplet-port` (`384c1ac`) | `3ed78fe` | present, plus `-flip` |

main's TideLink predates the KR260 chiplet FPGA targets entirely, so
`deploy_pair_role SOC=kr260_eth` has nothing to build and the whole bench flow
is dead on arrival. Re-point at `main` once that work merges.

### The two dies pin different TideLink commits

They do, today:

```
eth-chiplet      tidelink 3ed78fe
compute-chiplet  tidelink 3f3de09
```

That is legal — they are different designs with their own integration schedules
— but it is a **heterogeneous-pair hazard**, because a protocol-level skew
between the two ends looks exactly like a bad ribbon. `make deps` prints a
warning whenever the pins diverge, so it is visible at fetch time rather than at
2am on the bench. If the het pair fails to reach FCSM=4, check this first.

### Fetch policy

`make deps` deliberately does **not** recurse:

1. the two chiplet designs;
2. inside each of them, `tidelink` + `tidechart` only.

A blind `--init --recursive` pulls ~45 submodules eight levels deep across both
chiplets — the whole ASIC flow, the Arm IP wrappers, two nanoSoC SoCs — for a
repo whose primary deliverable is a pytest suite that needs none of it. Worse,
one submodule inside TideLink (`deps/tidelink-phy`) is declared over SSH at the
pinned commit, so a plain recursive init fails outright without SoTON keys.

`make deps-full` goes all the way down, delegating to each chiplet's *own*
`scripts/bootstrap.sh`, which already carries the SSH→HTTPS rewrite for that
nested clone. We do not reimplement that rewrite; the chiplet repo owns its own
fetch policy.

`scripts/bootstrap.sh` also runs `git submodule sync` before updating the nested
modules — the branch we pin declares TideLink over a *different remote* than
main does, and a stale URL in `.git/config` silently fetches the wrong line.

---

## 6. The flows

All of `flows/` are thin, documented wrappers. The machinery they call lives in
TideLink; forking it here would guarantee the two drift apart.

| Flow | Make target | What it does |
|---|---|---|
| `flows/bench_status.sh` | `bench-status` | read-only: fpgahub view, ssh reach, SoC/TideLink state |
| `flows/lease.sh` | `lease` / `release` | fpgahub lease on both boards, token recorded in `build/lease.env` |
| `flows/deploy_pair.sh` | `deploy-pair` | reflash both dies, **sequentially** |
| `flows/bringup_pair.sh` | `bench-bringup` | link bring-up on both dies, **concurrently** |
| `flows/recover.sh` | `bench-recover` | JTAG POR a wedged board |

### Two orderings that are not stylistic

**Deploy is sequential; bring-up is concurrent.** Deploy writes the PL, and two
`fpgautil` loads racing across a live ribbon is exactly the "never PL-reload one
side of a live link" hazard. Bring-up is the opposite: calibration only
completes when both ends train at once, because `cal_done` on each die gates on
the peer. Bring one side up, wait, then the other, and the first has given up.

**Bring-up is for FRESH dies only.** It drives `LL_SWRESET` on the way into data
mode. On an already-live link that resets one end under a running peer: the two
desync and the sender's next peer write hangs the PS bus. `flows/bringup_pair.sh`
pre-checks and refuses; use `--verify` to just read the state.

The correct sequence is always:

```
power-cycle both  ->  deploy die_a  ->  deploy die_b  ->  bring up both  ->  test
```

---

## 7. fpgahub: which commands work from where

There is a **CLI/daemon version skew** in this lab, and it dictates the shape of
`flows/recover.sh`. The `fpgahub` CLI installed on the dev host is newer than
the daemon running on `mapstone-dev`, and calls routes that daemon does not
serve.

| Command | From the dev host | Notes |
|---|---|---|
| `fpgahub status --json` | works | collection endpoint; the flows parse this |
| `fpgahub lease acquire` / `release` | works | collection endpoint |
| `fpgahub health` | works | |
| `fpgahub board reset` / `show` | **HTTP 404** | per-board endpoint — must run on `mapstone-dev` |
| `fpgahub actions run` / `list` | **HTTP 404** | same |
| `fpgahub pair list` / `chassis list` | **404 / traceback** | these subcommands do not exist on the daemon |

So `flows/recover.sh` dispatches the reset over ssh to `mapstone-dev`, where the
CLI matches its own daemon.

### Two further traps in recovery

1. **`board` means the GROUP on mapstone-dev's CLI.** `fpgahub board reset
   kr260_01` there resets every member sequentially and stops at the first
   failure — and since the LAN8720 topology added a `kr260_01_pl` member with no
   reset method at all, it dies before POR'ing the real board. Use `target`:

   ```bash
   ssh mapstone-dev 'fpgahub target reset kr260_01 --method default --yes'
   ```

   `--method default` is `kr260_jtag_por` ("the only reliable KR260 recovery — a
   soft reset wedges the PMU"). `--method reboot` is `kr260_kexec_reboot`, the
   gentler everyday path (`flows/recover.sh --soft`), and is **not** enough for
   an AXI-wedged board.

2. **A failed ssh is not a wedged board.** `ssh host true` exits 255 for a dead
   board *and* for a rejected key. `scripts/_common.sh:board_probe()` separates
   them — `unreachable` (nothing listening on :22) is the only verdict that may
   ever be read as "possibly wedged"; `auth`, `hostkey` and `degraded` all mean
   the board is alive. Neither `preflight.sh` nor `recover.sh` will POR a board
   that answers on :22. POR'ing a healthy board somebody else is using is the
   worst thing this tooling could do.

`jq` is **not** installed on the dev host, so every JSON parse in `scripts/` and
`flows/` goes through python. Do not add a `jq` dependency.

---

## 8. Preflight

`scripts/preflight.sh` is a prerequisite of every hardware make target.

```bash
./scripts/preflight.sh --offline   # tools + python env
./scripts/preflight.sh --single    # + board A reachable, lease free
./scripts/preflight.sh --pair      # + both boards
```

It checks tools, the venv, that `hetsoc` imports, that fpgahub is reachable,
that neither board is leased by somebody else, and that both answer ssh. It
never touches `/dev/mem`, the PL, or the peer aperture — the heaviest thing it
does on a board is `ssh <host> true`.

The point is that `make test-pair` with the bench powered down comes back in
seconds saying which board is dark, instead of hanging inside pytest. On this
bench "fail fast" is a safety property, not a nicety.

---

## 9. Results

```
build/results/pytest-l0.xml      one JUnit per level, from run_pytest.sh
build/results/*.json             bench-script summaries
build/results/junit.xml          the merge (make junit)
build/results/dashboard.html     level-by-level summary (make dashboard)
build/regress/*.log              per-step logs from regress.sh
```

`ci/results_to_junit.py` merges pytest XML and bench JSON into one file. The
bench JSON shape is the one TideLink's `kr260_eth_regress.py --json` already
emits (`{"pass": bool, "results": [{name, ok, detail, gating}]}`), so anything
that speaks it drops in with no adapter.

`ci/dashboard.py` renders levels rather than a raw pass count, and makes "not
run" visually distinct from "passed" — because L4 showing *not run* is the
correct state, and a wall of green must never be readable as "the data plane
works".

---

## 10. Environment

`set_env.sh` is sourced, idempotent, and assigns every variable with `:=` so an
existing value always wins. It sources `site.local.sh` **first** (gitignored;
copy `site.local.sh.example`) so per-rig overrides beat the committed defaults.

Key variables:

| Variable | Default | |
|---|---|---|
| `HETSOC_ROOT` | repo root | |
| `HETSOC_PYTHON` | newest ≥3.11, else ≥3.9 | the system `python3` here is 3.8 — too old |
| `HETSOC_VENV` | `.venv` | |
| `HETSOC_BOARD_A` / `_B` | `kr260_01` / `kr260_02` | fpgahub names |
| `HETSOC_BOARD_A_HOST` / `_B_HOST` | `ubuntu@10.22.24.159` / `.153` | verified against `fpgahub status --json` |
| `HETSOC_ROLE_A` / `_B` | `die_a` / `die_b` | straight / mirrored J21 pinout |
| `HETSOC_FPGAHUB_DEV_HOST` | `mapstone-dev` | where per-board endpoints resolve |
| `HETSOC_CONFIG` | `hetsoc.toml` if present | never set to a missing file |
| `HETSOC_PASSWORD` | unset | board ssh/sudo password; **site.local.sh only** |

`set_env.sh` does *not* re-source the chiplets' own `set_env.sh` scripts. Each
mutates `PATH` and points vendor-IP variables at the shared read-only lab tree
(`/research/AAA/**`), and sourcing several in sequence produces an environment
nobody can reason about. Flows that need those variables delegate to the
submodule's own Makefile, which sources its own environment.

**Never write under `/research/AAA/**`.** Those are shared, lab-wide vendor IP
trees that other engineers and CI builds depend on.

---

## 11. Lint

`make lint` runs three gates: ruff (or flake8), shellcheck, and a Makefile parse
check.

The **gating** ruff rule set is pinned in `scripts/lint.sh` to `--select E,F
--line-length 100`, not left to ruff's defaults, for two reasons:

1. ruff's default selection changes between releases, and a gate that silently
   widens on `pip install -U ruff` turns a green repo red for reasons nobody
   asked for — after which people stop running it;
2. `E`+`F` is the "this is a defect" tier. The modernisation rules (`UP*`) flag
   ~400 uses of `%`-formatting across `host/` and `tests/`, which is the house
   style throughout this lab. Failing a build over that is noise.

The wider set still runs as a **non-gating advisory** row, so the findings stay
visible.

shellcheck runs with `-x -P SCRIPTDIR -S style` — every script under
`scripts/`, `flows/` and `ci/` is clean at that level. `deps/`, `.venv/` and any
`build/` or `csrc/` directory are excluded: linting another tool's generated
shell is pure noise.
