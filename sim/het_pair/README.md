# `het_pair` — the heterogeneous pre-silicon pair

One real `nanosoc_eth_chiplet` die and one real `nanosoc_compute_chiplet` die in
**one simulation**, D2D pins tied together, driven by cocotb.

This is the pre-silicon gate for the two-board bring-up. Until now the ethernet
chiplet has only ever been paired with *itself*
(`nanosoc-ethernet-chiplet/verif/g2_soc_pair`, a die_a/die_b flip) and the
compute chiplet only ever with *itself*. Every asymmetry between the two designs
— address map, aperture base, inbound target set, TideLink revision, stimulus
port — is invisible to a homogeneous pair by construction. Those asymmetries are
exactly what will bite on a bench with two boards and a J21 ribbon.

## Status

Confirmed on this host (VCS T-2022.06-SP2, cocotb 1.7.2):

- **Elaboration passes** — 302 modules, 0 errors.
- **`test_smoke_harness` passes.**
- **Role negotiation and calibration work across the heterogeneous pair**: the
  two dies negotiate opposite roles from their straps with *zero pokes on the
  compute die*, and `calibration_done` asserts on both.

**Known blocker.** The Wlink FCSM then **stalls at state 1** on both dies and
never reaches 4, so the aperture never opens and the data-plane tests below
cannot pass yet. That is finding **F6** in `docs/SIM_PLAN.md` §8a, which records
the two hypotheses already tested and eliminated and the prime suspect
(`tidelink_top` hard-codes `apb_debug_unlock_i`/`mask_hs_bypass_i` to `1'b1`,
ignoring the chiplet ports). Do not re-try the manual LL bootstrap — it hangs
the AHB matrix when negotiation is armed (F5).

The data-plane tests below are written against the address maps in
`docs/SIM_PLAN.md` §6 and are correct-by-construction, but are blocked on F6.
See §8a for exactly what has and has not been observed, and for all six
findings the exercise produced.

## What it proves

| Test | Claim |
|---|---|
| `test_smoke_harness` | The ethernet die's `eth_ss_0` stimulus port reaches its own SRAM; the compute die boots and releases `sys_hresetn`; the compute die's **uncabled** link 1 stays down. |
| `test_het_link_brings_up` | The pair reaches **FCSM=4 and `cal_done` on both dies** with pokes on ONE side only, and the role resolves *oppositely* across two different designs (eth master / compute slave) from the straps. |
| `test_peer_write_eth_to_compute` | A peer write on the ethernet die lands in the **compute die's real `shared_sram_0`**, and reads back over the link. |
| `test_peer_sequence_eth_to_compute` | 8 consecutive words survive intact — catches cross-beat write/read misalignment a single access cannot. |
| `test_ipc_mailbox_eth_to_compute` | The IPC mailbox path, with the CAM rule the *heterogeneous* pair needs (`0x2F`→**`0x2A`**), not the one the homogeneous pair uses. |
| `test_cam_disabled_is_identity` | Control case: CAM off ⇒ the address arrives untranslated, so the translated bytes above demonstrably came from the CAM and not from identity passthrough. |
| `test_inbound_confinement_negative` | **The most valuable test here.** A CAM rule of `0x2F`→`0x23` is legitimate on an eth↔eth pair (it hits the far die's mailbox) and is a plausible copy-paste into a bring-up script. On *this* pair it must be **DECERRed**, because `0x23` is not in the compute SoC's inbound target set. A silent OKAY would be a cross-die confinement failure, and it is invisible to both repos' own testbenches. |

## What it does *not* prove

- **The compute→ethernet direction.** Programming the compute die's egress CAM
  needs a write to its TideLink APB, and the compute chiplet top exports no bus
  to write it with. See `docs/SIM_PLAN.md` §"Stimulus asymmetry".
- **Anything about the compute chiplet's own peer aperture.** Its window is
  based at `0x4000_0000`, so its peer aperture is `0x41......` — but its
  firmware targets `0x4000_0100`, which the shared decoder resolves as the *TX*
  aperture. That is a design defect recorded in the plan, not something this
  bench can paper over.
- **Two independent clock domains.** Both dies share `sys_fclk` and `ref_clk`
  here. On a bench each board has its own oscillator. A skewed-clock variant is
  the obvious next step.

## Running it

Needs **VCS** (or another simulator supporting Verilog library maps + config
blocks). Verilator cannot build this: it rejects duplicate module definitions
outright, which is precisely the problem this testbench has to solve.

```sh
# The gate — structural elaboration. Minutes.
make -C sim het-pair

# Against working checkouts rather than the pinned submodules:
make -C sim het-pair \
     ETH_CHIPLET_HOME=/path/to/nanosoc-ethernet-chiplet \
     COMPUTE_CHIPLET_HOME=/path/to/NanoSoC-Compute-Chiplet

# The cocotb tests. Long — a full two-SoC bring-up per test.
make -C sim sim
# one test only:
make -C sim/het_pair sim TESTCASE=test_inbound_confinement_negative
```

Useful knobs: `WAVES=1` (dumps `waves.vcd`), `SKID_BITS=N` (inserts N cycles of
pad delay each way, modelling ribbon flight time).

## Why the build looks unusual

The two SoCs are rendered by the same generator from different system
descriptions, so they contain **39 modules with the same name and different
content** — including `PHC_AHB` (different port lists) and `nanosoc_ss_cpu_plus`
(whose two copies disagree about `system_hreadyout`, the net behind the ethernet
die's stimulus port). Verilog has one global module namespace, so simply
concatenating both flists makes the netlist a property of declaration order.

`gen_libmap.py` therefore emits a Verilog **library map + config**: one library
per die plus a shared library for byte-identical vendor IP, with each die
instance bound to its own library. A liblist is inherited by the whole subtree
below an instance, so two rules partition both SoC hierarchies completely.

`make elab-naive` runs the same elaboration with the flists simply concatenated.
It is **expected to fail**, and it is kept so the claim above is reproducible
rather than asserted.

---

*A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
license. Copyright 2026, SoC Labs (www.soclabs.org).*
