"""L0-BUILD — static build-configuration gates. No board, no simulator, no cost.

Copyright (C) 2026, SoC Labs (www.soclabs.org)

WHY THIS FILE EXISTS
--------------------
Two defects were found on 2026-08-05 that between them make the heterogeneous
pair unable to come up on a bench — and BOTH are visible by reading source
files. Neither needed a board, a simulator, or an EDA licence to detect. Both
would have been discovered by two people, a ribbon, and a wasted afternoon.

    F7   the compute die cannot role-lock, so the link cannot reach FCSM=4
    H6   the compute images pair one role's ball map with the other's strap

Everything here is a parse of RTL / TCL / XDC. It runs in CI on every push. If
one of these ever fails again, it fails in seconds, in a pull request, with the
fix in the message — not on a bench.

See docs/FPGA_TEST_PROGRAMME.md §2.1 and docs/CHIPLET_ALIGNMENT_AUDIT.md.

WHAT `xfail(strict=True)` MEANS HERE
------------------------------------
Two of these describe defects that are OPEN in the compute repo today. They are
marked `xfail(strict=True)`, which is not the same as tolerating them:

  * the suite stays honest — a known, documented, externally-owned blocker does
    not turn this repo's CI permanently red, which is how gates get ignored;
  * `strict=True` means the test FAILS THE BUILD the moment the defect is fixed
    and nobody updated this file. The fix cannot land silently.

They are NOT xfailed to make a gate go green. The blocker is real and the
docstrings say so in the terms an operator needs: do not book bench time on the
het pair until L0-BUILD-01 passes.
"""
import os
import pathlib
import re

import pytest

pytestmark = pytest.mark.l0

REPO = pathlib.Path(__file__).resolve().parent.parent

# The chiplet checkouts. Default to the submodule pins this repo tracks; allow
# an override so the gates can be pointed at whatever tree actually built the
# bitstream under test (they are not always the same — see the note in
# docs/OVERNIGHT_REPORT.md about unpushed compute commits).
ETH = pathlib.Path(os.environ.get("ETH_CHIPLET_HOME", REPO / "deps" / "eth-chiplet"))
CMP = pathlib.Path(os.environ.get("COMPUTE_CHIPLET_HOME", REPO / "deps" / "compute-chiplet"))

ETH_TOP = ETH / "src" / "rtl" / "nanosoc_eth_chiplet.sv"
CMP_TOP = CMP / "src" / "rtl" / "nanosoc_compute_chiplet.sv"
CMP_WRAPPER = (CMP / "tidelink" / "fpga" / "vivado_ip"
               / "nanosoc_compute_chiplet_vivado_wrapper.v")
CMP_SOC_YAML = (CMP / "nanosoc-compute-system" / "sys_desc"
                / "nanosoc_compute_soc.yaml")
# Each die's FPGA target definitions live in ITS OWN tidelink checkout. Both
# checkouts happen to carry every target directory, but resolving each die from
# its own tree is what makes the comparison meaningful — otherwise a stale pin
# on one side silently supplies both halves of a "match" test.
ETH_TARGETS = ETH / "tidelink" / "fpga" / "targets"
CMP_TARGETS = CMP / "tidelink" / "fpga" / "targets"

# The pairing this bench runs: eth die_a against the non-flip compute image.
# (The -flip compute image has die_a's ball map, so it would drive the same
# conductors the eth die drives — see L0-BUILD-05.)
ETH_TARGET = "kr260-eth-chiplet"
CMP_TARGET = "kr260-compute-chiplet"


def _read(path):
    if not path.is_file():
        pytest.skip("not checked out: %s (run `make deps-full`)" % path)
    return path.read_text(errors="replace")


def _tidelink_top_params(top_text, inst):
    """The parameter override list on a `tidelink_top #(...) <inst>` instance.

    Returns the raw '#(...)' text, or "" when the instance is parameterless.
    """
    m = re.search(r"tidelink_top\s*#\s*\((.*?)\)\s*" + re.escape(inst),
                  top_text, re.S)
    return m.group(1) if m else ""


def _const_val(target, root):
    """CONFIG.CONST_VAL of the role_strap xlconstant in a target's BD script."""
    tcl = _read(root / target / "tidelink_design.tcl")
    m = re.search(r"CONFIG\.CONST_VAL\s*\{(\d+)\}\]\s*\$strap_const", tcl)
    if not m:
        pytest.skip("could not parse role_strap CONST_VAL from %s" % target)
    return int(m.group(1))


def _pin_map(target, root):
    """{port: PACKAGE_PIN} for the D2D pads of a target, from its XDCs."""
    out = {}
    tdir = root / target
    if not tdir.is_dir():
        pytest.skip("target dir absent: %s" % tdir)
    for xdc in sorted(tdir.glob("*.xdc")):
        for m in re.finditer(
                r"PACKAGE_PIN\s+(\w+)[^}]*\}\s*\[get_ports\s+\{?\s*"
                r"(pad_(?:clk_)?(?:tx|rx)[0-9_\[\]]*)\s*\}?\s*\]",
                xdc.read_text(errors="replace")):
            out.setdefault(m.group(2).strip(), m.group(1))
    if not out:
        pytest.skip("no pad_* PACKAGE_PIN lines found in %s" % tdir)
    return out


# ===========================================================================
# L0-BUILD-01 — the F7 gate.
# ===========================================================================
@pytest.mark.xfail(strict=True, reason=(
    "F7 OPEN: the compute die has no armed role-lock route, so the het link "
    "cannot reach FCSM=4. Fixed by a compute REBUILD with "
    "SELF_ARM_TRAIN_EN(1'b1). This xfail is strict — it will fail the build "
    "when the rebuild lands, which is the signal to update it."))
def test_l0_build_01_compute_can_role_lock():
    """L0-BUILD-01: the compute die must have at least one armed role-lock route.

    ★ THE GATE. Until this passes, DO NOT BOOK BENCH TIME on the het pair —
    the link cannot start, and every downstream symptom (no cal_done, no
    CR/CRACK, FCSM stuck at 0) will point at the ribbon instead.

    `role_locked` is a mutual clock enable, not a status bit:
        wire wlink_por_reset = ~poresetn | ~role_locked;
            (axi_chiplet_controller.sv:801, feeding .por_reset at :1575)
    so with it low the Wlink is held in POR and nothing can train.

    Exactly three terms can set `role_lock_reg`, and on the built compute image
    all of them are closed:

      route                     state on the compute image
      ------------------------  --------------------------------------------
      SW W1S of ROLE_CFG[1]     unreachable — ps_m omits d2d0/d2d1 (H2),
                                nanosoc_compute_soc.yaml:1110
      mask_hs_bypass_i          tied 1'b0, compute vivado wrapper :259
      nego_lost_w  (autoneg)    NEGO_CFG_RESET = 7'h00, never overridden
      SELF_ARM_TRAIN_EN         1'b0 — the tidelink_top default (:227);
                                compute does not override it (:698, :899)

    The eth die works precisely because it DOES override it:
        tidelink_top #(..., .SELF_ARM_TRAIN_EN(1'b1), ...) u_tidelink
            (nanosoc_eth_chiplet.sv:615)

    Note firmware does not rescue this: the gate is shut whoever issues the
    write, and `dap_m` is excluded from d2d0/d2d1 too.
    """
    cmp_top = _read(CMP_TOP)
    self_arm = "SELF_ARM_TRAIN_EN(1'b1)" in _tidelink_top_params(
        cmp_top, "u_tidelink_0").replace(" ", "")
    bypass = re.search(r"\.mask_hs_bypass_i_0\s*\(\s*1'b1\s*\)",
                       _read(CMP_WRAPPER)) is not None
    ps_m_has_d2d = _ps_m_reaches_d2d(_read(CMP_SOC_YAML))

    assert (self_arm or bypass) and ps_m_has_d2d, (
        "the compute die has NO armed role-lock route, so the het link cannot "
        "reach FCSM=4.\n"
        "  SELF_ARM_TRAIN_EN(1'b1) on u_tidelink_0 : %s\n"
        "  mask_hs_bypass_i_0 tied 1'b1            : %s\n"
        "  ps_m reaches d2d0                       : %s\n"
        "Ranked fixes:\n"
        "  1. REBUILD compute with .SELF_ARM_TRAIN_EN(1'b1) on u_tidelink_0 — "
        "mirrors the eth die's I1 fix (nanosoc_eth_chiplet.sv:615). One "
        "parameter, one Vivado turnaround, no security review.\n"
        "  2. Tie mask_hs_bypass_i_0 to 1'b1 in the FPGA wrapper — weakens the "
        "mask handshake; acceptable on a bench, not in general.\n"
        "  3. Add d2d0/d2d1 to ps_m — this is H2, a DELIBERATE down-link "
        "safety property. It needs review, and on its own it is NOT "
        "sufficient: it only reopens the software route.\n"
        "See docs/FPGA_TEST_PROGRAMME.md §0.1."
        % (self_arm, bypass, ps_m_has_d2d))


def _ps_m_reaches_d2d(yaml_text):
    """True if the ps_m initiator's target list contains d2d0."""
    # ps_m is the LAST initiator in the block, so a lookahead for "the next
    # initiator" never fires. Bound on the next entry at the SAME indent, or
    # end of file. (First cut used the lookahead and silently skipped — which
    # would have made the F7 gate look inapplicable rather than failing.)
    m = re.search(r"^(?P<i>\s*)-\s*name:\s*ps_m\b"
                  r"(?P<body>.*?)"
                  r"(?=^(?P=i)-\s*name:|\Z)",
                  yaml_text, re.S | re.M)
    if not m:
        pytest.skip("could not locate the ps_m initiator block in the compute yaml")
    return re.search(r"-\s*name:\s*d2d0\b", m.group("body")) is not None


# ===========================================================================
# L0-BUILD-04 / 05 — the H6 electrical hazard, split into its two halves.
# ===========================================================================
@pytest.mark.xfail(strict=True, reason=(
    "H6 OPEN: kr260-compute-chiplet carries die_b's ball map with die_a's "
    "strap (0). Fixed in the same compute rebuild as F7. Strict — it will "
    "fail the build when the rebuild lands."))
def test_l0_build_04_strap_matches_the_deployed_role():
    """L0-BUILD-04: each image's role strap must match the role it is deployed as.

    The bench pairs eth `kr260-eth-chiplet` (die_a, strap 0) with compute
    `kr260-compute-chiplet`, whose BALL MAP is die_b's — so its strap must be 1.
    It is 0 (tidelink_design.tcl:159), so both dies POR believing they are
    master and neither becomes the slave the link needs.

    The other image is not an escape: `-flip` has strap 1 but die_a's ball map,
    which is the contention case L0-BUILD-05 guards.
    """
    eth_strap = _const_val(ETH_TARGET, ETH_TARGETS)
    cmp_strap = _const_val(CMP_TARGET, CMP_TARGETS)
    assert eth_strap != cmp_strap, (
        "both deployed images carry role strap %d — the pair has two %ss and "
        "no %s, so role negotiation cannot resolve.\n"
        "  %-28s strap %d\n  %-28s strap %d\n"
        "The compute image's ball map is die_b's (L0-BUILD-05 confirms the "
        "conductors are complementary), so ITS strap is the wrong one: it "
        "should be 1. Fix in tidelink_design.tcl:159 CONFIG.CONST_VAL."
        % (eth_strap, "master" if eth_strap == 0 else "slave",
           "slave" if eth_strap == 0 else "master",
           ETH_TARGET, eth_strap, CMP_TARGET, cmp_strap))


def test_l0_build_05_ball_maps_are_complementary():
    """L0-BUILD-05: die A's TX conductors must be die B's RX conductors.

    THE ELECTRICAL ONE. If both images drive the same ball, two 3.3 V CMOS
    outputs at DRIVE 8 fight across the ribbon. KR260_BENCH_RUNBOOK.md already
    forbids it — "Same image on both boards drives two outputs onto every
    ribbon lane — never do it" — and this catches it before power is applied,
    which a runbook sentence cannot.

    Expected to PASS for the chosen pairing: `kr260-compute-chiplet` is the
    mirrored (die_b) map. It is the STRAP that is wrong on that image, not the
    pinout — which is exactly why these are two separate tests. Selecting
    `-flip` to fix the strap would trade a dead link for contention.
    """
    eth_pins = _pin_map(ETH_TARGET, ETH_TARGETS)
    cmp_pins = _pin_map(CMP_TARGET, CMP_TARGETS)

    def _clk(pins, direction):
        for port, ball in pins.items():
            if re.fullmatch(r"pad_clk_%s(_0)?" % direction, port):
                return port, ball
        pytest.skip("no pad_clk_%s port found (ports: %s)"
                    % (direction, sorted(pins)))

    eth_tx_port, eth_tx = _clk(eth_pins, "tx")
    eth_rx_port, eth_rx = _clk(eth_pins, "rx")
    cmp_tx_port, cmp_tx = _clk(cmp_pins, "tx")
    cmp_rx_port, cmp_rx = _clk(cmp_pins, "rx")

    assert cmp_tx != eth_tx, (
        "CONTENTION HAZARD — both images transmit on ball %s:\n"
        "  %s %s = %s\n  %s %s = %s\n"
        "Two CMOS drivers on one ribbon conductor. Do NOT deploy this pairing. "
        "Use the image whose ball map MIRRORS the eth die."
        % (eth_tx, ETH_TARGET, eth_tx_port, eth_tx,
           CMP_TARGET, cmp_tx_port, cmp_tx))
    assert (cmp_tx, cmp_rx) == (eth_rx, eth_tx), (
        "forwarded clocks are not complementary:\n"
        "  eth  TX %s  RX %s\n  cmp  TX %s  RX %s\n"
        "Expected the compute die's TX on the eth die's RX ball and vice versa."
        % (eth_tx, eth_rx, cmp_tx, cmp_rx))


# ===========================================================================
# L0-BUILD-02 / 07 — parity facts worth carrying into every report.
# ===========================================================================
def test_l0_build_07_lane_counts_match():
    """L0-BUILD-07: both dies must instantiate the same NUM_PHY_LANES."""
    def lanes(text, name):
        m = re.search(r"NUM_PHY_LANES\s*=\s*(\d+)", text)
        if not m:
            pytest.skip("NUM_PHY_LANES not found in %s" % name)
        return int(m.group(1))
    e, c = lanes(_read(ETH_TOP), "eth top"), lanes(_read(CMP_TOP), "compute top")
    assert e == c, ("lane-count mismatch: eth %d, compute %d — the ribbon "
                    "cannot carry a pair that disagrees on width" % (e, c))


@pytest.mark.nongating
def test_l0_build_02_recovery_parity_is_recorded(record_property):
    """L0-BUILD-02: record whether the two dies ship the same FC recovery logic.

    NON-GATING by intent. `AUTO_ANCHOR_EN` is set on the eth die and absent on
    compute, so re-anchoring is eth-driven only. That is a fact to carry into
    every wedge report — not a regression to block a merge on. Asserting it
    would make the gate permanently red for something neither repo has agreed
    to change yet.
    """
    eth_anchor = "AUTO_ANCHOR_EN(1'b1)" in _tidelink_top_params(
        _read(ETH_TOP), "u_tidelink").replace(" ", "")
    cmp_anchor = "AUTO_ANCHOR_EN(1'b1)" in _tidelink_top_params(
        _read(CMP_TOP), "u_tidelink_0").replace(" ", "")
    record_property("auto_anchor_eth", eth_anchor)
    record_property("auto_anchor_compute", cmp_anchor)
    if eth_anchor != cmp_anchor:
        print("\n  [L0-BUILD-02] AUTO_ANCHOR_EN asymmetry: eth=%s compute=%s "
              "— re-anchoring is driven by one die only. Carry this into any "
              "wedge analysis." % (eth_anchor, cmp_anchor))
