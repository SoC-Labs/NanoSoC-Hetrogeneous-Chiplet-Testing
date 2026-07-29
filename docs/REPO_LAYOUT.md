# Repo layout contract

This file is the **authoritative ownership map** for the repo. It exists so
parallel work (human or agent) lands in disjoint areas. If you add a file,
add it here.

## Ownership

| Area | Owns | Must NOT write |
|---|---|---|
| **Plan** | `docs/VERIFICATION_PLAN.md`, `docs/TEST_MATRIX.md`, `docs/ARCHITECTURE.md` | anything else |
| **Flows** | `Makefile`, `set_env.sh`, `.gitmodules`, `.gitignore`, `scripts/`, `flows/`, `ci/`, `.github/`, `docs/CI.md` | `docs/VERIFICATION_PLAN.md`, `host/`, `tests/`, `sim/` |
| **Framework** | `host/hetsoc/**`, `host/pyproject.toml`, `host/README.md` | `tests/`, `docs/*` (except `host/README.md`) |
| **Tests** | `tests/**` | `host/hetsoc/**` (consume only), `docs/*` |
| **Bench** | `docs/BENCH_RUNBOOK.md`, `docs/SAFETY.md`, `docs/BRINGUP_GAPS.md`, `fpgahub/` | `host/`, `tests/`, `sim/` |
| **Sim** | `sim/**`, `docs/SIM_PLAN.md` | everything else |

`README.md` and `docs/REPO_LAYOUT.md` are owned by the integrator (top level).

## The three test surfaces

1. **`sim/`** — pre-silicon. Two chiplet RTL tops instantiated back-to-back with
   the D2D pins tied together, driven by cocotb. Catches protocol/decode bugs
   before a board is touched. No hardware.
2. **`host/hetsoc/` + `tests/`** — on-silicon. Drives two real KR260s over SSH,
   poking the chiplet SoC through each board's PS backdoor window. This is the
   primary deliverable.
3. **`flows/` + `ci/`** — automation around both.

## Test level convention (used by `tests/` and the matrix)

| Level | Needs | Wedge risk | Runs in CI |
|---|---|---|---|
| **L0** | nothing (pure host logic: address maths, registry, guards) | none | yes, always |
| **L1** | 1 board, read-only probes | none | yes, nightly |
| **L2** | 1 board, config-plane writes (role, CAM) | low | yes, nightly |
| **L3** | 2 boards, link bring-up + control plane | low | yes, nightly |
| **L4** | 2 boards, cross-die **data plane** | **HIGH** — known intermittent wedge | **no** — attended only |
| **L5** | 2 boards, soak / stress / characterisation | **HIGH** | no — attended only |

L4/L5 are opt-in behind an explicit flag. See `docs/SAFETY.md`.

## Naming

- pytest files: `tests/test_l<N>_<area>.py`; test ids `L<N>-<AREA>-<NN>`.
- Test ids in `docs/TEST_MATRIX.md` must match the pytest ids exactly.
