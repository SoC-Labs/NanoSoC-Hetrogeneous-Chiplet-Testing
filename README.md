# NanoSoC Heterogeneous Chiplet Testing

Test framework for **die-to-die (D2D) communication and functionality between the
NanoSoC Ethernet Chiplet and the NanoSoC Compute Chiplet**, running as a
heterogeneous pair across two Xilinx KR260 boards joined by a J21 ribbon.

> **Status: bring-up.** This repo is the *test framework and verification plan*.
> The heterogeneous (eth ↔ compute) pair has **not** been run on silicon yet —
> see [`docs/BRINGUP_GAPS.md`](docs/BRINGUP_GAPS.md) for what blocks it. What
> *has* been proven on silicon is the **homogeneous** eth-chiplet pair
> (die_a + die_b of the same design): TideLink link up (FCSM=4) and cross-die
> SRAM/mailbox transfers, 2026-07-27..29.

## What this is

The eth-chiplet repo ([`NanoSoC-Ethernet-Chiplet`](https://github.com/SoC-Labs/NanoSoC-Ethernet-Chiplet))
grew a working but ad-hoc set of bench scripts (`kr260_eth_regress.py`,
`kr260_eth_xfer.py`, `kr260_eth_bringup.py`, …) that prove the *eth↔eth* pair.
This repo generalises that into a **design-agnostic, two-node chiplet test
framework**:

| | eth-chiplet repo (today) | this repo |
|---|---|---|
| Pair | homogeneous (eth die_a ↔ eth die_b) | **heterogeneous** (eth ↔ compute) |
| Address map | hard-coded 3× per script | one **target descriptor registry** |
| Tests | shell modes + a bespoke runner | **pytest** suite, layered L0–L5 |
| Safety | comments in a runbook | **enforced in code** (`hetsoc.safety`) |
| Scope | one design | any TideLink D2D chiplet pair |

## Layout

| Path | What |
|---|---|
| [`docs/VERIFICATION_PLAN.md`](docs/VERIFICATION_PLAN.md) | the comprehensive plan — layers, coverage, exit criteria |
| [`docs/TEST_MATRIX.md`](docs/TEST_MATRIX.md) | every test ID, what it proves, status |
| [`docs/BENCH_RUNBOOK.md`](docs/BENCH_RUNBOOK.md) | operator steps for the two-board bench |
| [`docs/SAFETY.md`](docs/SAFETY.md) | wedge hazards and recovery — **read before touching a board** |
| [`docs/BRINGUP_GAPS.md`](docs/BRINGUP_GAPS.md) | what must exist before the het pair can run |
| [`host/hetsoc/`](host/hetsoc/) | the Python test framework (targets, boards, pair, safety) |
| [`tests/`](tests/) | the pytest suite (L0 offline → L5 soak) |
| [`sim/`](sim/) | pre-silicon heterogeneous pair simulation |
| [`flows/`](flows/) | build/deploy/regress flows |
| [`ci/`](ci/) | CI entry points and result publishing |

## Quick start

```bash
source set_env.sh
make deps           # sub-repos + python env
make test-offline   # L0: no boards needed
make bench-status   # L1: lease + probe both boards (read-only, wedge-safe)
```

Full bench flow: [`docs/BENCH_RUNBOOK.md`](docs/BENCH_RUNBOOK.md).

## Safety — the one rule

The KR260 PS reaches the chiplet SoC **only** through a narrow backdoor window.
**Any PS read of a PL address the SoC does not decode hangs the ZynqMP AXI bus
with no timeout** — the board drops to 100% packet loss and only a JTAG POR
recovers it. The framework refuses out-of-window addresses in code; do not
bypass it, and never point a bare-link script (`kr260_smoke.py`, `tl39.py`,
`kr260_credit_tx.py`) at a chiplet target. See [`docs/SAFETY.md`](docs/SAFETY.md).

## Licence

Copyright (C) 2026, SoC Labs (www.soclabs.org)
