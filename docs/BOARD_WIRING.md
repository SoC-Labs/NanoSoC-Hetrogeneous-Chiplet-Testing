# Board wiring — the two-board heterogeneous bench

Physical setup for the eth-chiplet ↔ compute-chiplet pair on two Xilinx KR260s.

> **Status tags.** **[BUILT]** = in a bitstream that exists today, pin assignment
> DRC-checked by a passing `build_design`. **[PROPOSED]** = pin choice is sound,
> never built. **[BLOCKED]** = cannot be wired because the artefact does not exist
> — see [`BRINGUP_GAPS.md`](BRINGUP_GAPS.md).
>
> **Everything in this document describes the eth-chiplet bitstreams.** The compute
> chiplet has no KR260 bitstream and therefore no pinout — §7 is the delta that must
> be established once it does. Read [`SAFETY.md`](SAFETY.md) before powering
> anything.

---

## 0. The bench

```
             ┌────────────────────────── mapstone-dev ──────────────────────────┐
             │  fpgahub daemon (/run/fpgahub/fpgahub.sock)                      │
             │  JTAG/FTDI POR cables · lab mgmt net 10.22.24.0/24 · SWD probes  │
             └───┬──────────────────────────────────────────────────────────┬───┘
                 │ mgmt eth (PS GEM)                        mgmt eth (PS GEM) │
                 │ 10.22.24.159  kr260-01                   10.22.24.153 kr260-02
   ┌─────────────┴──────────────┐                       ┌──────────────┴─────────────┐
   │   KR260  #1  "die_a"       │                       │   KR260  #2  "die_b"       │
   │   fpgahub: kr260_01        │                       │   fpgahub: kr260_02        │
   │   image: kr260-eth-chiplet │                       │   image: kr260-compute-… ? │
   │   role strap 0 (master)    │                       │   role strap 1 (slave)     │
   │                            │                       │            [BLOCKED]       │
   │  J21 (RPi 40)              │                       │              J21 (RPi 40)  │
   │  ┌──────────┐              │                       │              ┌──────────┐  │
   │  │ 18 lanes ├══════════════╪═══ ribbon, straight ══╪══════════════┤ 18 lanes │  │
   │  │ +2 I²C   │  TX↔RX crossed BY THE FLIP BUILD,    │              │ +2 I²C   │  │
   │  │ +4 GND   │  NOT by the cable. BCM_n ↔ BCM_n.    │              │ +4 GND   │  │
   │  └──────────┘  ⚠ phys 1,2,4,17 (+3V3/+5V) STRIPPED │              └──────────┘  │
   │                            │                       │                            │
   │  PMOD1  LAN8720 [PROPOSED] │                       │  PMOD1  LAN8720 [PROPOSED] │
   │  PMOD2  SWD 1-3 + TX1 @ 4  │                       │  PMOD2  SWD 1-3 + TX1 @ 4  │
   │  PMOD3  LEDs 3-4           │                       │  PMOD3  LEDs 3-4           │
   │  PMOD4  1.8 V — KEEP CLEAR │                       │  PMOD4  1.8 V — KEEP CLEAR │
   │  barrel jack (own PSU)     │                       │  barrel jack (own PSU)     │
   └────────────────────────────┘                       └────────────────────────────┘
        ▲ SWD probe (ST-Link/DAPLink)                        ▲ SWD probe
        └── to the host running OpenOCD                      └── to the host running OpenOCD
```

Both boards are **plain Ubuntu, no PYNQ**. Each powers itself from its own barrel
jack — the ribbon carries **no power**.

---

## 1. Bill of materials

| Item | Qty | Notes |
|---|---:|---|
| Xilinx KR260 | 2 | `kr260_01` (10.22.24.159) / `kr260_02` (10.22.24.153) in fpgahub |
| RPi-40 ribbon, J21↔J21, **straight-through** | 1 | phys **1, 2, 4, 17 stripped** — see §3.3 |
| 3.3 V SWD probe (ST-Link / DAPLink) | 2 | on **PMOD2** pins 1–3 |
| Dev host with the probes | 1 | `mapstone-dev` reaches both boards over the mgmt net |
| Waveshare LAN8720 RMII module | 2 | *M2 / ethernet milestone only* — not needed for D2D |
| Spare host NIC ports on `mapstone-dev` | 2 | *M2 only*; one per PL-ethernet segment. **Confirm these exist before allocating subnets.** |

---

## 2. Voltage domains — read before plugging anything in

The KR260 PMOD connectors are **not all the same voltage**. Proven the hard way:
SWD on PMOD4 at `LVCMOS33` failed placement with
`DRC BIVB-1 — LVCMOS33 is not supported for banks of type High Performance`.

| Connector | Balls (pins 1,2,3,4 / 7,8,9,10) | Bank type | **Vcco** |
|---|---|---|---|
| PMOD1 | H12, E10, D10, C11 / B10, E12, D11, B11 | HD | **3.3 V** |
| PMOD2 | J11, J10, K13, K12 / H11, G10, F12, F11 | HD | **3.3 V** |
| PMOD3 | AE12, AF12, AG10, AH10 / AF11, AG11, AH12, AH11 | HD | **3.3 V** |
| **PMOD4** | L2, T7, AF7, AF6 / AD7, W10, Y10, AB10 | **HP (64/65)** | **1.8 V** |
| **J21** RPi header | §3 | HDIO bank 44 | **3.3 V** |

Pmod numbering: top row `1 2 3 4 5=GND 6=VCC`, bottom row `7 8 9 10 11=GND 12=VCC`.
Signals are physical 1–4 and 7–10.

> **Driving 3.3 V into a PMOD4 pin can damage the SOM.** PMOD4 is unused on both
> builds. Keep it that way.

---

## 3. The J21 ribbon — the D2D link

### 3.1 The cable is STRAIGHT-THROUGH

There is **no crossover in the cable**. Every conductor is `BCM_n ↔ BCM_n`, pin 27
to pin 27. The **flip build swaps the TX and RX balls in its XDC**, which is what
makes each conductor exactly one driver against one receiver.

> ⚠️ **Same image on both boards shorts two outputs on all 18 lanes.** Always pair a
> `die_a` image with a `-flip` (`die_b`) image. See [`SAFETY.md`](SAFETY.md) H6.

### 3.2 Conductor table — 18 signal lanes + 2 I²C **[BUILT]**

`phys` = RPi 40-pin header physical pin (KR260 J21). Balls are from
`tidelink/fpga/targets/kr260-eth-chiplet{,-flip}/kr260_eth_chiplet_tidelink.xdc`.

| BCM | phys | die_a ball | die_a signal | die_b ball | die_b signal | Carries |
|----:|-----:|---|---|---|---|---|
| 0 | 27 | AD15 **HDGC** | `pad_clk_tx` → | AD15 | `pad_clk_rx` ← | **a→b forwarded clock** |
| 1 | 28 | AD14 | `pad_tx[0]` → | AD14 | `pad_rx[0]` ← | a→b lane 0 |
| 9 | 21 | AC13 | `pad_tx[1]` → | AC13 | `pad_rx[1]` ← | a→b lane 1 |
| 12 | 32 | AA13 | `pad_tx[2]` → | AA13 | `pad_rx[2]` ← | a→b lane 2 |
| 13 | 33 | AB13 | `pad_tx[3]` → | AB13 | `pad_rx[3]` ← | a→b lane 3 |
| 4 | 7 | AG14 | `pad_tx[4]` → | AG14 | `pad_rx[4]` ← | a→b lane 4 |
| 5 | 29 | AH14 | `pad_tx[5]` → | AH14 | `pad_rx[5]` ← | a→b lane 5 |
| 6 | 31 | AG13 | `pad_tx[6]` → | AG13 | `pad_rx[6]` ← | a→b lane 6 |
| 7 | 26 | AH13 | `pad_tx[7]` → | AH13 | `pad_rx[7]` ← | a→b lane 7 |
| 8 | 24 | AC14 **HDGC** | `pad_clk_rx` ← | AC14 | `pad_clk_tx` → | **b→a forwarded clock** |
| 16 | 36 | AB15 | `pad_rx[0]` ← | AB15 | `pad_tx[0]` → | b→a lane 0 |
| 17 | 11 | AB14 | `pad_rx[1]` ← | AB14 | `pad_tx[1]` → | b→a lane 1 |
| 10 | 19 | AE13 | `pad_rx[2]` ← | AE13 | `pad_tx[2]` → | b→a lane 2 |
| 11 | 23 | AF13 | `pad_rx[3]` ← | AF13 | `pad_tx[3]` → | b→a lane 3 |
| 14 | 8 | W14 | `pad_rx[4]` ← | W14 | `pad_tx[4]` → | b→a lane 4 |
| 15 | 10 | W13 | `pad_rx[5]` ← | W13 | `pad_tx[5]` → | b→a lane 5 |
| 18 | 12 | Y14 | `pad_rx[6]` ← | Y14 | `pad_tx[6]` → | b→a lane 6 |
| 19 | 35 | Y13 | `pad_rx[7]` ← | Y13 | `pad_tx[7]` → | b→a lane 7 |
| 2 | 3 | — | `i2c_sda_io` ↔ | — | `i2c_sda_io` ↔ | TideLink I²C sideband (open-drain) |
| 3 | 5 | — | `i2c_scl_io` ↔ | — | `i2c_scl_io` ↔ | TideLink I²C sideband (open-drain) |

**What each group carries.** Two independent **source-synchronous** groups, one per
direction: 8 data lanes plus the transmitter's own forwarded clock. The receiver
recovers data against *the far die's* clock — `pad_clk_rx` is asynchronous to
everything local and **only toggles when the far die is powered and transmitting**.
Both forwarded clocks land on **HDGC (global-clock-capable)** balls (`AD15`, `AC14`)
so the received clock reaches a BUFG with no `CLOCK_DEDICATED_ROUTE` override; they
sit at phys **27 and 24**, three pins apart, which keeps clock-vs-data skew small.

Link rate is **3.125 MHz** (25 MHz reference, `/8`).

**Not on the ribbon:** UART is on BCM20/21 (phys 38/40) — see §5. There is no
per-lane framing: the same 8 lanes carry the config plane, the AXI data plane and
the FC sideband, multiplexed by TideLink.

**Known risk — `BCM0`/`BCM1` are `ID_SD`/`ID_SC`**, the RPi HAT-ID EEPROM pins. If
the carrier fits pull-ups there they load the forwarded clock and lane 0. At
3.125 MHz a push-pull `DRIVE 8` output swamps a few-kΩ pull-up, but it is the first
thing to suspect if lane 0 or the clock looks sick while lanes 1–7 are clean.
Six other HDGC balls are free (`BCM12`/AA13, `BCM13`/AB13, `BCM16`/AB15,
`BCM17`/AB14, `BCM9`/AC13, `BCM1`/AD14) — moving the clock pair to `BCM12`/`BCM16`
is an XDC-only change on both dies plus a rebuild.

**`BCM2`/`BCM3` are I²C1** and need pull-ups. Confirm the carrier fits them; if
not, add ~2.2 kΩ to 3V3 **on one board only**.

### 3.3 Power and ground — the destructive mistake

| Rail | J21 phys pins | Action |
|---|---|---|
| **+3V3** | **1, 17** | 🔴 **STRIP / DO NOT BRIDGE** |
| **+5V** | **2, 4** | 🔴 **STRIP / DO NOT BRIDGE** |
| GND | 6, 9, 14, 20, 25, 30, 34, 39 | ✅ bridge **at least four** |

Each KR260 powers itself from its own barrel jack. Tying two independently
regulated supplies together can back-feed a regulator and damage **both** boards.
**A full 40-way straight ribbon bridges all four rails** — so either use a partial
loom, or physically remove conductors **1, 2, 4 and 17**.

Interleaved ground returns matter more than count. Suggested: 9↔9, 14↔14, 25↔25,
39↔39 — pin 25 sits between the BCM9/BCM11 group and pin 30 sits near BCM6/BCM12.

A partial loom is fine **provided every phys pin in §3.2 is bridged**.

> A previous revision of the ancestor doc claimed a 26-way ribbon could not reach
> phys 27 and that this caused a dead link. **That was refuted on hardware
> (2026-07-17)** — die_b recovered an epoch anchor on the phys-27 conductor. All 8
> balls conduct both ways and the ribbon is continuity-tested good. Do not revive
> it. The conductor and keep-out guidance above is unaffected.

### 3.4 Plug-in order

1. Both boards **powered off**.
2. Plug the ribbon. **Confirm pin-1 orientation on *both* ends** — J21 pin 1 is
   silkscreened; a reversed connector puts 5 V onto BCM19.
3. Power on die_a, then die_b.
4. Deploy the die_a image to one board and the `-flip` image to the other.
5. Verify the role straps read back correctly (`ROLE_STATUS` at SoC `0x2E03_2084`)
   **before** touching the link — it catches a swapped bitstream in one second.

---

## 4. SWD probe — PMOD2, 3.3 V **[BUILT]**

Optional. The PS-side bring-up path needs no probe; SWD is for firmware work and
core-level debug.

| Signal | PMOD2 pin | Ball | Direction |
|---|---|---|---|
| `SWCLK` | 1 | J11 | probe → FPGA |
| `SWDIO` | 2 | J10 | bidirectional |
| `SWD_NPORESETN` | 3 | K13 | probe → FPGA (optional; SWD `SYSRESETREQ` works without it) |
| GND | 5 / 11 | — | probe return |
| VREF (**3.3 V**) | 6 / 12 | — | probe VREF sense |

```
  Probe            KR260 PMOD2  (3.3 V)
  ---------------------------------------
  SWCLK    ------> pin 1   (J11)
  SWDIO    <-----> pin 2   (J10)
  nRESET   ------> pin 3   (K13)   optional
  GND      ------- pin 5 or 11
  VREF/VTG <------ pin 6 or 12     must read 3.3 V
```

The SoC is strapped **SWD-only** (`dap_swj_enable = 1`, JTAG TAP tied off), so
`transport select swd` is the only valid transport. `SWCLK` carries
`CLOCK_DEDICATED_ROUTE FALSE` on its **net** (applying it to a *port* throws
`Netlist 29-69` and fails the message gate).

```bash
openocd -f nanosoc-multicore-system/pynq/scripts/openocd/nanosoc_multicore.cfg
#   default SWD_INTERFACE=interface/stlink.cfg
#   DAPLink: -c "set SWD_INTERFACE interface/cmsis-dap.cfg"
```

Keep `SWCLK` short. If the probe is flaky, drop `adapter speed 1000` first — a long
flying-lead SWCLK is the usual culprit.

> The board's own micro-USB JTAG reaches the **ZynqMP/PL config TAP only**. It
> cannot debug the soft Cortex-M0+. That is also the cable fpgahub's
> `kr260_jtag_por` plugin drives for POR recovery — a different function on the
> same connector.

---

## 5. UART console

**Current state [BUILT]:** on two spare J21 pins, off the ribbon.

| Signal | BCM | J21 phys | Ball |
|---|---|---|---|
| `uart_txd` | 20 | 38 | W12 |
| `uart_rxd` | 21 | 40 | W11 |

Two better options, neither built:

- **Option A [PROPOSED, preferred]** — route the SoC UART into PS UART1 via EMIO so
  it appears as `/dev/ttyPS1` alongside the Linux console on `/dev/ttyPS0`. Proven
  on the PYNQ-Z2 flow. Needs `PSU__UART1__PERIPHERAL__{ENABLE,IO}` in the block
  design, the external `uart_txd`/`uart_rxd` BD ports and XDC lines **deleted**, and
  a device-tree overlay applied at deploy.
- **Option B [PROPOSED]** — 3.3 V USB-UART dongle on PMOD3 pins 7 (AF11, → dongle
  **RXD**) and 8 (AG11, → dongle **TXD**), GND on pin 11. **Cross TX↔RX.** Do not
  connect the dongle's VCC when the board is powered.

The KR260 micro-USB FTDI UART channel is wired to the **PS MIO** and carries the
Linux console. It is **not connected to PL pins** — no XDC constraint can put a PL
signal on it.

Move off the J21 pins before the two-board demo; they are better kept clear of the
ribbon.

---

## 6. LAN8720 RMII PHY — the later ethernet milestone **[PROPOSED]**

Not needed for D2D. The pins place and route; **no LAN8720 has ever been physically
fitted or driven**, and the M1 build deliberately ties the PHY off inside
`nanosoc_eth_chiplet_vivado_wrapper.v` (`rmii_*_idle` constants). Wiring a real PHY
means undoing those tie-offs and promoting the signals IP-port → BD-port → XDC —
that is the bulk of the work; the pin choice is the easy part.

9 signals, 8 pins per Pmod ⇒ **TX1 is the single overflow**, one flying lead.

| Signal | Connector | Pin | Ball | Module pin |
|---|---|---|---|---|
| `rmii_rxd[1]` | PMOD1 | 1 | H12 | RX1 |
| `rmii_txd[0]` | PMOD1 | 2 | E10 | TX0 |
| `rmii_crs_dv` | PMOD1 | 3 | D10 | CRS_DV |
| `mdc_pad_o` | PMOD1 | 4 | C11 | MDC |
| `rmii_tx_en` | PMOD1 | 7 | B10 | TX_EN |
| `rmii_rxd[0]` | PMOD1 | 8 | E12 | RX0 |
| `MDIO` (bidir) | PMOD1 | 9 | D11 | MDIO |
| `rmii_ref_clk` | PMOD1 | 10 | B11 | nINT/REF_CLK (50 MHz out) |
| **`rmii_txd[1]`** | **PMOD2** | **4** | **K12** | **TX1 — overflow lead** |
| 3.3 V / GND | PMOD1 | 6,12 / 5,11 | — | module power |

The Waveshare module's pinout is **fixed by the board — do not re-order it.**
Status LEDs currently sit on PMOD1 pins 1–2 and **must move to PMOD3 pins 3–4**
(AG10/AH10) when the PHY takes PMOD1. There is no `phy_nrst`: the module
self-resets.

**Gotchas.** `RXD0`/`RXD1`/`CRS_DV` double as `PHYAD[2:1]`/`MODE0` and are sampled
at reset — if MDIO reads all-ones or all-zeros, suspect the PHY address first.
`nINT/REFCLKO` is dual-function; confirm which mode your module ships in. Keep RMII
leads short — 50 MHz on flying leads is already marginal. Power the module from the
same PMOD it signals to.

**Hub side — already done.** Each PL-ethernet segment is its own fpgahub board
entry, and both already exist with distinct `/24`s and a **unique `pl_mac` per
die** (verified live 2026-07-29):

| | `kr260_01_pl` | `kr260_02_pl` |
|---|---|---|
| host NIC | `192.168.20.1/24` | `192.168.21.1/24` |
| board IP | `192.168.20.101` | `192.168.21.101` |
| `pl_mac` | `02:00:5e:00:20:01` | `02:00:5e:00:21:01` |
| capability | `ethernet_phy_lan8720` | `ethernet_phy_lan8720` |

The distinct `pl_mac` matters because both boards run the same SoC image — an
unset one would put two identical MACs on the network.

**So the hub is not the blocker.** What is missing is physical (no LAN8720 has
ever been fitted) and firmware: neither chiplet has ethernet firmware — MDIO PHY
bring-up, the MAC taken out of internal loopback, a PicoTCP instance. See
[`../fpgahub/README.md`](../fpgahub/README.md) §5.

Cheapest early check: fit the module and cable it — **the PHY link LED alone proves
the RMII pinout and the 3.3 V bank**, before any firmware.

---

## 7. What differs on the compute board — **[BLOCKED / TBD]**

Everything above is the **eth-chiplet**. The compute chiplet has **no `fpga/`
directory, no Vivado project, no XDC and no KR260 bitstream**, so **it has no board
pinout at all** ([`BRINGUP_GAPS.md`](BRINGUP_GAPS.md) G1). What *is* known, from
the chip-boundary specs:

### What the RTL says (known)

| Aspect | ETH | COMPUTE | Consequence for the bench |
|---|---|---|---|
| **Ribbon shape per link** | 1 fwd clk + 8 TX, 1 fwd clk + 8 RX (`NUM_PHY_LANES = 8`) | **identical** | ✅ the §3.2 conductor table should carry over 1:1 |
| **Number of TideLinks** | 1 (`d2d_*`) | **2** (`d2d0_*`, `d2d1_*`) | ⚠️ a KR260 has one J21 — **link 1 must be tied off** (G8) |
| Pad names | `d2d_clk_tx`, `d2d_tx[8]`, … | `d2d0_clk_tx`, `d2d0_tx[8]`, … + `d2d1_*` | naming only |
| I²C sideband | `i2c_scl`/`i2c_sda` bidir, OE active-low | **same shape**, ×2 | ✅ BCM2/BCM3 carry over |
| `role_strap` | **tied `1'b0`** in the boundary; a Vivado `xlconstant` on the FPGA build | **bonded input pad**, ×2 | ⚠️ compute has a pin but **no FPGA driver** (G6) |
| `user_ref_clk` | **not bonded** — aliased onto `sys_fclk` | **bonded pad**, ×2 | ⚠️ different clocking (G15) |
| SWD / JTAG | SWD only (`dap_swj_enable = 1`, JTAG tied off) | full SWJ-DP (`swd_clk`, `swd_dio`, `jtag_tdi`, `jtag_tdo`, `jtag_ntrst`) | PMOD2 pinout would need 3 more pins for JTAG, or use SWD only |
| UART | not bonded on the ASIC boundary | `uart_rxd` / `uart_txd` bonded | — |
| **Ethernet RMII + MDIO** | bonded (6 pads) | **absent** | ✅ **§6 is eth-side only** — the ethernet milestone does not apply to the compute board |
| `phc_pps_out` | open | bonded | — |

### What only a compute FPGA build can answer

| Question | Must be answered by | Status |
|---|---|---|
| Does the compute build use the **same J21 ball map**, so the same straight ribbon works? | a compute-side `*_tidelink.xdc` | **BLOCKED** |
| Does it take the **flip** (die_b) ball map or the straight one? | the role assignment + XDC | **BLOCKED** |
| Where do SWD / UART / LEDs land? | a compute-side XDC | **BLOCKED** |
| Is there a PS backdoor window, and at what base/size? | a compute-side block design — **and a SoC port that does not exist yet** (G2) | **BLOCKED** |
| Where is link 1 terminated? | a compute-side wrapper | **BLOCKED** (G8) |

**The wiring assumption to validate first:** the per-link ribbon shape is already
identical, so if the compute FPGA wrapper is built to the **same J21 ball map with
the flip (die_b) assignment**, the ribbon, the strip list, the ground returns and
the plug-in order in §3 all carry over unchanged, and this document needs only a
compute column added to §3.2. **That is the cheapest thing to specify into the
compute FPGA build — say it before the XDC is written, not after.**

---

## 8. Pre-power checklist

```
[ ] ribbon straight-through, phys 1/2/4/17 (+3V3,+5V) stripped or absent
[ ] >= 4 ground conductors bridged, interleaved
[ ] pin-1 orientation confirmed on BOTH J21 ends
[ ] die_a image on one board, -flip (die_b) image on the other — NOT the same image
[ ] nothing 3.3 V is touching PMOD4 (1.8 V)
[ ] SWD probe VREF reads 3.3 V on PMOD2 pin 6/12
[ ] UART dongle (if fitted) TX<->RX crossed, VCC NOT connected
[ ] LAN8720 (if fitted) powered from the PMOD it signals to; LEDs moved off PMOD1
[ ] each board on its OWN barrel-jack PSU
```

---

## References

- Ribbon + balls: `tidelink/fpga/targets/kr260-eth-chiplet{,-flip}/kr260_eth_chiplet_tidelink.xdc`,
  `tidelink/fpga/targets/kr260-pair-nptp/ribbon_wiring.md` (eth-chiplet repo)
- Pin/bank data: Vivado 2024.1 `kr260_som` / `kr260_carrier` board files;
  `nanosoc-multicore-system/.../pynq_kr260/fpga_pinmap.xdc`
- Pad-level spec: eth-chiplet `docs/PIN_MAP.md`, `docs/PHYSICAL_HANDOFF.md`
- Reset/clock constraints on the D2D pads: eth-chiplet `docs/RESET_ORDERING.md` §2, §3
- OpenOCD/SWD: `nanosoc-multicore-system/pynq/scripts/openocd/nanosoc_multicore.cfg`
