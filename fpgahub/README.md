# fpgahub integration

Lets the two-board heterogeneous pair be leased, deployed, probed and recovered
through the lab board-management tool.

| File | What |
|---|---|
| [`fpgahub.toml`](fpgahub.toml) | the per-project manifest — artefacts, actions, capability gate |

Read [`../docs/SAFETY.md`](../docs/SAFETY.md) before dispatching anything against
a board. Operator flow: [`../docs/BENCH_RUNBOOK.md`](../docs/BENCH_RUNBOOK.md).

---

## 1. Board inventory — the real values

Read live from the daemon on `mapstone-dev` (2026-07-29). Do not guess these.

| Target | Role | Chassis | Hub | Net IF | Board IP | Hostname | `host.ssh` | Capabilities |
|---|---|---|---|---|---|---|---|---|
| `kr260_01` | **die_a** | `kr260_01` | `1-1.2.3` | `kr260_01_ps` | **10.22.24.159** | `kr260-01` | `ubuntu@10.22.24.159` | `zynqmp_jtag_por, jtag_ftdi, swd_stlink, uart_console` |
| `kr260_02` | **die_b** | `kr260_02` | `1-1.2.2` | `kr260_02_ps` | **10.22.24.153** | `kr260-02` | `ubuntu@10.22.24.153` | `zynqmp_jtag_por, jtag_ftdi, swd_stlink, uart_console` |
| `kr260_01_pl` | — | `kr260_01` | `2-1.2.3.1` | `kr260_01_pl` | 192.168.20.101 | `kr260-01-pl` | **`null`** | `ethernet_phy_lan8720` |
| `kr260_02_pl` | — | `kr260_02` | `2-1.2.2.2` | `kr260_02_pl` | 192.168.21.101 | `kr260-02-pl` | **`null`** | `ethernet_phy_lan8720` |

`host.proxy` is `null` on all four (renders as `""` — it is the one optional
token). `host.dev_host` is `mapstone-dev` for the two PS entries.

**Each KR260 appears twice.** `/api/v1/groups` reports each as its own 2-member
*chassis*, `[kr260_0N_pl, kr260_0N]` — **`_pl` first**. That ordering is what
breaks the group reset (§6). The `_pl` entries carry the per-die `pl_mac`
(`02:00:5e:00:20:01` / `02:00:5e:00:21:01`) for the later ethernet milestone;
they are **not** used for D2D work.

---

## 2. Three constraints that will bite you

Established against the live daemon. All three are encoded in the manifest's
header comment too — this section says *why*.

### 2.1 `{pair.*}` is gone — and the ancestor manifest no longer loads

fpgahub moved from the **board/chassis/pair** model to **board/target/link**. The
`pair` namespace was removed. Known namespaces are exactly:

```
artefact, board, chassis, firmware, host, link, manifest, port, script
```

Every `tidelink/fpga/fpgahub.toml` in the lab — including the eth-chiplet's —
now fails to load:

```
action:deploy_pair.command: unknown token namespace 'pair' in {pair.local.role}.
```

**Consequence:** the ancestor runbook's
`fpgahub actions run kr260_01 deploy_kr260_eth_chiplet_pair` **does not work
today**. It is documented `[PROVEN]` there because it was, before the upgrade.
That is the hole this manifest fills.

### 2.2 `{link.*}` does not resolve either — roles are literals

`{link.*}` is the replacement for `{pair.*}`, but it needs a `[links.<id>]`
block, and **there is none joining `kr260_01` and `kr260_02`**. `/api/v1/groups`
shows two independent chassis. A `{link.local.role}` token raises
`board 'kr260_01' is not in a link`.

So the manifest has **one deploy action per role**, with `ROLE=die_a` /
`ROLE=die_b` as literals, and the operator dispatches the right one at the right
board. If an operator later adds a `[links.het_pair]` block with the two boards
as members, these can collapse back to a single role-aware action.

### 2.3 Never bind actions to the `_pl` targets

`kr260_01_pl` / `kr260_02_pl` have `host.ssh = null`. A `{host.ssh}` token there
raises `token {host.ssh} resolved to None — board state is incomplete`. The
manifest's project-level `[requires] capabilities = ["zynqmp_jtag_por"]` gates
these actions to the PS entries, which do declare it — the `_pl` entries declare
only `ethernet_phy_lan8720`.

---

## 3. Registering the manifest

**This is an operator edit on `mapstone-dev`**, where the daemon lives. There is
no env var, no symlink directory and no `fpgahub apply` for this —
`fpgahub apply` regenerates *udev rules*, not manifests.

### 3.1 Validate first (do this before asking anyone)

```bash
/home/dam1n19/SoCLabs/fpgahub/.venv/bin/python -c "
import sys; sys.path.insert(0,'/home/dam1n19/SoCLabs/fpgahub/src')
from fpgahub import manifest as M
m = M.load('<abs path>/fpgahub/fpgahub.toml')
print('OK', m.project, len(m.actions), 'actions')
"
```

Current result: `OK nanosoc-het-chiplet-testing 6 actions`. The loader uses
`extra="forbid"` on every model, so an unknown key anywhere is a hard error —
this check is worth running after every edit.

### 3.2 Two prerequisites on the daemon host

`/etc/fpgahub/config.toml`:

```toml
[daemon]
allow_manifest_exec = true                 # default is false; without it every
                                           # dispatch returns 403
manifest_roots      = ["/home/david/SoCLabs"]   # every manifest_path must
                                           # resolve (symlinks followed) inside
                                           # one of these
```

⚠️ **`manifest_roots` is why the checkout location matters.** The live boards'
manifests all resolve under `/home/david/SoCLabs/…` on `mapstone-dev`. **This
repo must be checked out under a path inside `manifest_roots`**, or the binding
is refused. Confirm the actual roots with the operator before cloning.

### 3.3 Bind it to both boards

Add this repo's manifest to each board's allowlist:

```toml
[boards.kr260_01]
server   = "mapstone-dev"
hub_path = "1-1.2.3"
manifest_paths = [
    "/home/david/SoCLabs/NanoSoC-Hetrogeneous-Chiplet-Testing/fpgahub/fpgahub.toml",
]
default_manifest = "nanosoc-het-chiplet-testing"

[boards.kr260_02]
server   = "mapstone-dev"
hub_path = "1-1.2.2"
manifest_paths = [
    "/home/david/SoCLabs/NanoSoC-Hetrogeneous-Chiplet-Testing/fpgahub/fpgahub.toml",
]
default_manifest = "nanosoc-het-chiplet-testing"
```

`manifest_path` (singular) and `manifest_paths` (list) are **mutually
exclusive** — use the list; it is the preferred form and allows switching
projects per board later. Active-manifest resolution order is: runtime selection
in `/var/lib/fpgahub/state.json` → `default_manifest` → first entry in
`manifest_paths` → nothing (404).

**Neither board has any manifest bound today:**

```
GET /api/v1/targets/kr260_01/manifest -> {"detail":"board 'kr260_01' has no manifest bound"}
```

### 3.4 Seed the deploy secret

The deploy actions declare `secret_env = { KR260_PASSWORD = "kr260.ssh_password" }`.
That is a **logical** name resolved from the board's `[secrets]` block:

```toml
[boards.kr260_01.secrets]
"kr260.ssh_password" = "file:/var/lib/fpgahub/secrets/kr260_01/kr260_ssh_password"
```

```bash
sudo install -d -m 0700 -o root -g root /var/lib/fpgahub/secrets/kr260_01
sudo install -m 0600 /dev/null /var/lib/fpgahub/secrets/kr260_01/kr260_ssh_password
sudo bash -c 'printf "%s" "<ubuntu password>" > /var/lib/fpgahub/secrets/kr260_01/kr260_ssh_password'
```

File contents are used verbatim; a trailing `\n` is stripped and nothing else is
normalised. Secrets never appear in argv and are never journalled.

> **Better: skip the secret entirely.** Stage SSH keys
> (`ssh-copy-id ubuntu@10.22.24.159`) and NOPASSWD sudo on the boards, and leave
> `kr260.ssh_password` unset — the deploy path falls back to key auth and
> `sudo -n`.

### 3.5 Reload

```bash
sudo systemctl reload fpgahubd            # or, without service access:
fpgahub manifest reload kr260_01
fpgahub manifest reload kr260_02
```

Then confirm:

```bash
fpgahub manifest list kr260_01            # expect nanosoc-het-chiplet-testing active
fpgahub actions list kr260_01
```

> ⚠️ `manifest list` / `actions list` are **per-board endpoints** and 404 from a
> client running an older CLI. Run them on `mapstone-dev`, or curl the socket
> (§6).

---

## 4. Using the actions

| Action | Level | Board | Wedge risk |
|---|---|---|---|
| `test_offline` | L0 | either (no board touched) | none |
| `deploy_eth_die_a` | — | **`kr260_01`** | none (canaries suppressed) |
| `deploy_eth_die_b` | — | **`kr260_02`** | none |
| `bench_status` | L1 | dispatch **once**, on `kr260_01` | none — read-only |
| `ci_deploy_probe_die_a` | composite | `kr260_01` | none |
| `por_recover` | recovery | the wedged board | destructive (clears the PL) |

```bash
fpgahub lease acquire kr260_01 && fpgahub lease acquire kr260_02

fpgahub actions run kr260_01 deploy_eth_die_a
fpgahub actions run kr260_02 deploy_eth_die_b
fpgahub actions run kr260_01 bench_status

fpgahub actions run kr260_01 por_recover --confirm    # only if wedged
```

Useful flags on `actions run`:

| Flag | Effect |
|---|---|
| `--confirm` | required for actions marked `confirm = true` (`por_recover`) |
| `--force-prereqs` | bypass the `requires` flag gate — e.g. `bench_status` when the bitstreams were loaded outside this daemon session |
| `--json` | emit the final `RunStatus` as JSON |
| `--no-stream` | suppress the live log tail |

Exit codes: `ok=0`, `failed=1`, `rejected=3`, `timeout=124`, `cancelled=130`.

**Notes on dispatch semantics**, worth knowing before scripting against it:

- **No lease is required to dispatch.** If one is held it is *auto-extended* to
  cover `timeout_s`, never shortened. Take leases anyway — they are what stops
  another engineer reflashing your board mid-run.
- **One action per target at a time** (per-target mutex); a second dispatch
  returns `409` with `running_run_id`.
- **Flags are cleared on daemon restart**, and a failed action does not set its
  flags. That is what `--force-prereqs` is for.
- A USB detach kills the subprocess (`state=cancelled`,
  `reason="hotplug_detach"`).

> 🔴 **Bring-up and cross-die transfers are deliberately NOT fpgahub actions.**
> Link bring-up needs **both boards driven concurrently** (each die's `cal_done`
> gates on the peer over the ribbon), which a per-board action cannot express —
> it is orchestrated host-side. And the cross-die data plane (L4/L5) is
> **attended-only** because it wedges intermittently
> ([`../docs/SAFETY.md`](../docs/SAFETY.md) H3); an fpgahub action is by
> definition unattended dispatch. Do not add one.

---

## 5. What's already done on the hub side

The `_pl` PL-ethernet topology work is **already applied** — it was a planned
item in the eth-chiplet repo and has landed:

| | `kr260_01_pl` | `kr260_02_pl` |
|---|---|---|
| host NIC | `192.168.20.1/24` | `192.168.21.1/24` |
| board IP | `192.168.20.101` | `192.168.21.101` |
| **`pl_mac`** | `02:00:5e:00:20:01` | `02:00:5e:00:21:01` |
| capability | `ethernet_phy_lan8720` | `ethernet_phy_lan8720` |

Distinct `/24`s and **distinct `pl_mac` per die** — which matters because both
boards run the same SoC image, and an unset `pl_mac` would put two identical MACs
on the network.

**So the hub is not the ethernet blocker.** What is missing is physical (no
LAN8720 has ever been fitted) and firmware (neither chiplet has ethernet
firmware: MDIO bring-up, MAC out of internal loopback, a PicoTCP instance). See
[`../docs/BOARD_WIRING.md`](../docs/BOARD_WIRING.md) §6.

---

## 6. The two recovery quirks, precisely

### Quirk A — per-board endpoints 404 from this client host

**Cause: a client/daemon version skew, not the host.** The daemon serves
per-target routes at `/api/v1/targets/{name}/…`. Older CLI builds still call
`/api/v1/boards/{name}/…`, which no longer exists as a per-leaf route.
Collection endpoints (`status`, `board list`, `lease …`) and the board-level
lease routes survived the rename, which is why those still work from anywhere.

Reproduced from this host:

```
$ fpgahub board show kr260_01
GET /boards/kr260_01 -> HTTP 404: Not Found
$ fpgahub actions list kr260_01 --json
GET /boards/kr260_01/actions -> HTTP 404: Not Found
```

**Fix: run it on `mapstone-dev`,** where the CLI matches the daemon:

```bash
ssh mapstone-dev 'fpgahub board reset kr260_01 --yes'
# -> POR issued ... method=default plugin=kr260_jtag_por, via local (cable ...)
```

**Or upgrade this host's CLI** — the skew, not the host, is the problem.

### Quirk B — the group `board reset` breaks on the `_pl` member

`fpgahub board reset <chassis>` fans out over **every** chassis member and
**stops on first failure**. `/api/v1/groups` lists `kr260_0N_pl` **first**, and
that target has `capabilities = ["ethernet_phy_lan8720"]` with no reset config —
so `dispatch_reset` raises `board 'kr260_01_pl' has no reset method 'default'`,
the loop breaks, and **the real `kr260_0N` is never reset.**

(The POR plugin declares `required_capabilities = ("zynqmp_jtag_por",)`, and the
capability check is permissive only when a board's capability list is *empty* —
the `_pl` list is non-empty, so it fails hard.)

**Fix: POST to the single target.** From a client host:

```bash
ssh mapstone-dev "curl -s --unix-socket /run/fpgahub/fpgahub.sock \
  -X POST http://localhost/api/v1/targets/kr260_01/reset \
  -H 'Content-Type: application/json' \
  -d '{\"method\":\"default\",\"confirm\":true}'"
```

Or just dispatch this manifest's action, which does exactly that from inside the
daemon host and so needs no ssh hop:

```bash
fpgahub actions run kr260_01 por_recover --confirm
```

**Either way: POR one board at a time, ~8 s apart.** Back-to-back PORs hit a
transient "cable not found" on the second board — **retry once**. A POR clears
the PL, so redeploy both boards afterwards.

### Useful raw routes

All under the `/api/v1` prefix. Handy when the client CLI is skewed:

| Method | Path |
|---|---|
| `GET` | `/api/v1/health` |
| `GET` | `/api/v1/status`, `/api/v1/boards`, `/api/v1/groups` |
| `GET` | `/api/v1/targets/{name}` · `/status` · `/flags` |
| `GET` `POST` | `/api/v1/targets/{name}/reset` — POST body `{"method": <str\|null>, "confirm": <bool>}`, `extra="forbid"` |
| `GET` | `/api/v1/targets/{name}/manifest` · `/manifests` · `/actions` |
| `POST` | `/api/v1/targets/{name}/manifest/reload` |
| `POST` | `/api/v1/targets/{name}/actions/{action_id}` — body `{"dry_run": bool, "force_prereqs": bool}`, returns **202** |
| `GET` | `/api/v1/targets/{name}/actions/{run_id}` — poll for `RunStatus` |
| `GET`/`POST`/`DELETE` | `/api/v1/targets/{name}/lease` · `/api/v1/boards/{chassis}/lease` |

Dispatch status codes: `202` dispatched · `403` `allow_manifest_exec` is false ·
`404` unknown board or no manifest bound · `409` another action is running ·
`422` unknown action id or unmet `requires`.

> ⚠️ **Known cosmetic bug:** the `202` response's `status_url` is built as
> `/api/v1/boards/{name}/actions/{run_id}` — a **stale path that 404s**. Poll
> `/api/v1/targets/{name}/actions/{run_id}` instead.

---

## 7. Rules for editing this manifest

1. **Never `{pair.*}`.** Never `{link.*}` until a `[links.*]` block exists for
   these two boards.
2. **Never `{host.ssh}` / `{host.dev_host}`** in an action you might dispatch
   against a `_pl` target — they are `null` there and raise at render.
3. **Never `id = "reset"` or `id = "program"`.** With `method == "default"`, a
   manifest action named `reset` **overrides** the board-level `kr260_jtag_por`
   plugin — silently hijacking the recovery path. Same shape of trap for
   `program`.
4. **Never `produces = ["board_up"]`** — it is daemon-owned and the loader
   rejects it. The three daemon-settable flags you may `require` without
   producing are `board_up`, `bitstream_loaded`, `firmware_loaded`.
5. **Don't repeat a project-level capability** in an action's
   `requires_capabilities` — the loader rejects the redundancy.
6. **Declared `[scripts.<id>]` files must exist and be `+x` at load time**, or
   the whole manifest fails to load. Same for the executable bit.
7. **Set `timeout_s` explicitly** — the default is only **600 s**, which is short
   for a Vivado build. `revoke_grace_s` defaults to **10.0**; raise it for long
   SSH or soak actions.
8. **Exactly one** of `command` / `steps` / `sd_install` per action.
9. `cwd` is unset throughout, so it defaults to the manifest's **parent**
   directory — the repo root. Relative `-C` paths resolve from there. Keep it
   that way; it is why `make -C deps/eth-chiplet/tidelink/fpga …` works.
10. **Re-run the §3.1 validation after every edit.** `extra="forbid"` means one
    stray key breaks the whole file.

---

## References

- Loader (authoritative schema): `/home/dam1n19/SoCLabs/fpgahub/src/fpgahub/manifest.py`
- fpgahub `docs/MANIFESTS.md` (binding, tokens, dispatch), `UPGRADING.md` (the
  pair → link rename), `docs/BOARD_CONTROLS.md` (program/reset plugins)
- Good manifest templates that **do** load: `ahb_qspi/fpga/fpgahub.toml`
  (minimal), `ethernet-subsystem-ahb/fpga/fpgahub.toml` (richest — `[identity]`,
  `[scripts]`, `requires_capabilities`, `sd_install`)
- ❌ Do **not** copy any `tidelink/fpga/fpgahub.toml` — they no longer load (§2.1)
- [`../docs/SAFETY.md`](../docs/SAFETY.md) · [`../docs/BENCH_RUNBOOK.md`](../docs/BENCH_RUNBOOK.md) · [`../docs/BRINGUP_GAPS.md`](../docs/BRINGUP_GAPS.md) G10
