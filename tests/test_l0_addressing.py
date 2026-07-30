"""L0 — pure host logic: address maths, window guards, CAM encoding, registry.

**Runs with no hardware at all.** No board, no `pynq`, no `/dev/mem` (API
contract non-negotiable #4; L0-ADDR-15 asserts it). This is the layer that must
be green in CI on every commit.

Why this level carries more weight here than usual: the *heterogeneous*
eth<->compute pair has never run on silicon, and the compute chiplet has no
KR260 bitstream — its descriptor is provisional and refuses to produce a host
address at all. So L0 is currently the **only** place the heterogeneous address
maths can be checked. Every CAM rule the het pair will need is derived and
verified here against a Python model of `tl_addr_trans_cam.sv`.

Copyright (C) 2026, SoC Labs (www.soclabs.org)
"""
from __future__ import annotations

import itertools
import os
import sys

import pytest

from hetsoc import regs
from hetsoc.safety import AddressGuardError, ProvisionalTargetError
from hetsoc.targets import (BARE_LINK_TRAP_RANGES, INBOUND_IPC_MAILBOX,
                            INBOUND_SHARED_SRAM, NO_PEER_APERTURE, TARGETS,
                            get_target)

import _helpers as H

pytestmark = [pytest.mark.l0]

ALL_TARGETS = sorted(TARGETS)
CHIPLETS = sorted(n for n, t in TARGETS.items() if t.kind == "chiplet")
RESOLVED = sorted(n for n, t in TARGETS.items() if t.resolved)
PROVISIONAL = sorted(n for n, t in TARGETS.items() if not t.resolved)
#: Targets that can ORIGINATE a cross-die access (they have a peer aperture).
SENDERS = sorted(n for n, t in TARGETS.items()
                 if t.kind == "chiplet" and t.peer_aperture != NO_PEER_APERTURE)
#: Targets that can RECEIVE one (they declare inbound D2D regions).
RECEIVERS = sorted(n for n, t in TARGETS.items()
                   if t.kind == "chiplet" and t.inbound_targets)

# The rule words proven on silicon / in the two chiplets' sims.
KNOWN_RULES = [
    pytest.param(0x2F, 0x2D, 0x002D2F01, id="eth-peer-to-shared-sram"),
    pytest.param(0x2F, 0x23, 0x00232F01, id="eth-peer-to-eth-mailbox"),
    pytest.param(0x2F, 0x2A, 0x002A2F01, id="eth-peer-to-compute-mailbox"),
    pytest.param(0x40, 0x2D, 0x002D4001, id="compute-sim-peer-to-shared-sram"),
]


# ===========================================================================
# Target registry
# ===========================================================================

def test_l0_addr_01_target_registry_is_self_consistent():
    """L0-ADDR-01: every registered Target describes a usable die.

    Proves the registry cannot ship a half-filled descriptor. A target whose name
    disagrees with its key, whose peer aperture is not an address byte, or which
    declares an inbound region the far die's initiator does not decode would put
    every downstream address quietly wrong — and a wrong PS address on ZynqMP is
    an unrecoverable bus hang, not an exception.
    Pass: name == key; non-negative window; peer aperture is a byte or the
    explicit NO_PEER_APERTURE sentinel; every inbound byte is a byte; a
    provisional target says why it is provisional.
    """
    assert TARGETS, "the target registry is empty — no chiplet is describable"
    for name, target in sorted(TARGETS.items()):
        assert target.name == name, (
            "TARGETS[%r].name is %r — key and name must agree, or get_target() "
            "and Board(target=...) resolve to different descriptors"
            % (name, target.name))
        assert target.window_base >= 0 and target.window_size >= 0
        assert (target.peer_aperture == NO_PEER_APERTURE
                or 0 <= target.peer_aperture <= 0xFF), (
            "%s: peer_aperture 0x%X is neither an address byte nor the explicit "
            "'no peer window' sentinel. The CAM replaces addr[31:24] only."
            % (name, target.peer_aperture))
        for key, byte in target.inbound_targets.items():
            assert 0 <= byte <= 0xFF, (
                "%s: inbound %r is 0x%X, not an address byte" % (name, key, byte))
        if not target.resolved:
            assert target.provisional_reason, (
                "%s has no verified address window but gives no reason. A "
                "descriptor that refuses to translate must say what is unknown "
                "and what would resolve it." % name)


@pytest.mark.parametrize("name", ALL_TARGETS)
def test_l0_addr_02_get_target_round_trips(name):
    """L0-ADDR-02: `get_target(name)` returns the registry entry; an unknown name
    raises KeyError.

    Proves a typo in a config file (`target = "kr260-eth-chplet"`) fails loudly
    at setup instead of silently resolving to a default map. There is no safe
    fallback here: a guessed window base is an unrecoverable AXI hang.
    Pass: identity for a known name; KeyError for an unknown one; and passing a
    Target through is idempotent.
    """
    assert get_target(name) is TARGETS[name]
    assert get_target(TARGETS[name]) is TARGETS[name]
    with pytest.raises(KeyError):
        get_target(name + "-definitely-not-a-target")


# ===========================================================================
# The window guard
# ===========================================================================

@pytest.mark.parametrize("name", sorted(set(RESOLVED) & set(CHIPLETS)))
def test_l0_addr_03_to_host_is_a_clean_base_strip(name):
    """L0-ADDR-03: for a resolved chiplet target, `to_host(A)` == window_base + A.

    Proves the PS backdoor is a clean base-strip aperture — PS phys
    0x4_0000_0000 + A reaches SoC HADDR A (tidelink.hwh:4112 MEMRANGE, eth_ss_0).
    Any offset or scaling in that mapping would land every register access
    somewhere else in the SoC, and the SoC's default slave would answer with
    plausible-looking SLVERR data rather than an error the test could see.
    Pass: exact equality at the boot ROM, the TideLink APB, the CAM, the peer
    aperture and both inbound regions.
    """
    target = TARGETS[name]
    probes = [0x0, 0x4,
              target.reg(regs.SWI_LANE_STATUS),
              target.reg(regs.CAM_RULE_0),
              target.tidechart(regs.TC_STATUS)]
    if target.peer_aperture != NO_PEER_APERTURE:
        probes.append(target.peer(H.XFER_OFFSET))
    probes += [target.inbound_soc_base(k) for k in target.inbound_targets]

    for addr in probes:
        assert target.to_host(addr) == target.window_base + addr, (
            "%s: to_host(0x%08X) = 0x%X, expected window_base + addr = 0x%X. The "
            "backdoor is a clean base-strip, not an offset map."
            % (name, addr, target.to_host(addr), target.window_base + addr))


@pytest.mark.parametrize("name", RESOLVED)
def test_l0_addr_04_to_host_refuses_out_of_window(name):
    """L0-ADDR-04: `to_host()` raises AddressGuardError outside the window.

    The single most important safety property in the repo. An out-of-window PS
    read hangs the ZynqMP AXI bus with **no timeout** — the board goes to 100%
    packet loss and only a JTAG POR recovers it. There must be no unchecked path
    to /dev/mem.
    Pass: negative addresses, the first address past the window, a wildly
    out-of-range address, and a non-int all raise.
    """
    target = TARGETS[name]
    for bad in (-1, -0x1000, 1 << 40, 1 << 48):
        with pytest.raises(AddressGuardError):
            target.to_host(bad)
    if not target.apertures:
        for bad in (target.window_size, target.window_size + 4):
            with pytest.raises(AddressGuardError):
                target.to_host(bad)
    for bad in (None, "0x1000", 1.5, True):
        with pytest.raises(AddressGuardError):
            target.to_host(bad)


@pytest.mark.parametrize("name", PROVISIONAL)
def test_l0_addr_05_provisional_targets_refuse_to_translate(name):
    """L0-ADDR-05: a target with no verified window refuses every address.

    This is the heterogeneous pair's current state, encoded. The compute chiplet
    has no KR260 bitstream, no `.hwh`, and no external AHB pad group — so there
    is no PS-visible window base to declare. The correct behaviour is to refuse,
    loudly, rather than guess: a guessed base on ZynqMP is an unrecoverable hang,
    and "the test failed" is infinitely cheaper than "the board needs a JTAG POR
    and nobody knows why".
    Pass: `to_host()` raises ProvisionalTargetError for every address including
    0, and the error names what is unknown.
    """
    target = TARGETS[name]
    for addr in (0x0, 0x4, target.reg(regs.SWI_LANE_STATUS), 0x2D001000):
        with pytest.raises(ProvisionalTargetError) as excinfo:
            target.to_host(addr)
        assert target.provisional_reason[:40] in str(excinfo.value), (
            "the refusal must explain what is unresolved; got: %s"
            % excinfo.value)


@pytest.mark.parametrize("name", sorted(set(RESOLVED) & set(CHIPLETS)))
def test_l0_addr_06_never_emits_a_bare_link_address(name):
    """L0-ADDR-06: no address a chiplet target accepts maps into a bare-link
    aperture.

    Proves the framework cannot reproduce the failure that wedged kr260_01 on
    2026-07-27. The bare-link tooling (`tl39.py`, `kr260_credit_tx.py`,
    `kr260_smoke.py`, `kr260_afi.sh`) pokes PS 0x8000_0000 / 0x8403_xxxx /
    0xA400_xxxx; on a chiplet bitstream those are UNDECODED PL addresses with no
    SmartConnect responder, so a read there hangs the bus forever.

    The structural mechanism is that a chiplet backdoor is an HPM0_FPD *high*
    aperture above 4 GiB — this test asserts the property actually holds across
    the whole window, including when the bare-link literals are themselves
    offered as SoC addresses (the exact confusion the guard exists to survive).
    Pass: every produced PS address is >= 4 GiB, inside the window, and outside
    every bare-link trap range.
    """
    target = TARGETS[name]
    step = max(1, target.window_size // 512)
    sweep = list(range(0, target.window_size, step))[:512]
    sweep += [0, 4, target.window_size - 4, target.reg(regs.SWI_LANE_STATUS)]
    sweep += [base for _n, base, _s in BARE_LINK_TRAP_RANGES]

    for addr in sweep:
        if not 0 <= addr < target.window_size:
            with pytest.raises(AddressGuardError):
                target.to_host(addr)
            continue
        host = target.to_host(addr)
        assert host >= (1 << 32), (
            "%s: to_host(0x%08X) produced the 32-bit PS address 0x%08X. A "
            "chiplet backdoor is a HIGH aperture; anything below 4 GiB is DDR or "
            "undecoded PL." % (name, addr, host))
        assert target.window_base <= host < target.window_end
        for aperture, base, size in BARE_LINK_TRAP_RANGES:
            assert not (base <= host < base + size), (
                "%s: to_host(0x%08X) = 0x%X lands in the UNDECODED bare-link "
                "'%s' aperture. A PS read there hangs the AXI bus with no "
                "timeout." % (name, addr, host, aperture))


@pytest.mark.parametrize("name", SENDERS)
def test_l0_addr_07_is_peer_classifies_the_aperture_exactly(name):
    """L0-ADDR-07: `is_peer()` is true across the peer aperture and false
    everywhere else.

    Proves the classification that decides whether `require_link_up()` runs
    first. A false negative lets a peer access through ungated, and a peer access
    on a down link hangs the PS bus. A false positive gates harmless config reads
    behind a live link — which would make a die whose link will not come up
    impossible to diagnose, precisely when those registers matter most.
    Pass: true across the full 16 MB aperture; false for the TideLink APB, the
    CAM, the boot ROM, both inbound regions, and for junk input.
    """
    target = TARGETS[name]
    for offset in (0, 4, H.XFER_OFFSET, 0xFFFFFC):
        addr = target.peer(offset)
        assert target.is_peer(addr), (
            "%s: is_peer(0x%08X) is False, but 0x%02X is this target's peer "
            "aperture" % (name, addr, target.peer_aperture))

    not_peer = [0x0, 0x4, target.reg(regs.SWI_LANE_STATUS),
                target.reg(regs.CAM_RULE_0), target.tidechart(regs.TC_STATUS)]
    not_peer += [target.inbound_soc_base(k) + H.XFER_OFFSET
                 for k in target.inbound_targets]
    for addr in not_peer:
        assert not target.is_peer(addr), (
            "%s: is_peer(0x%08X) is True, but that is a LOCAL address (config "
            "plane or an inbound region). Gating it on link-up would make a down "
            "link undiagnosable." % (name, addr))
    for junk in (-1, None, "0x2F001000", True):
        assert not target.is_peer(junk)


# ===========================================================================
# CAM rule encoding
# ===========================================================================

@pytest.mark.parametrize("match,replace,expect", KNOWN_RULES)
def test_l0_addr_08_cam_rule_matches_the_proven_words(match, replace, expect):
    """L0-ADDR-08: `regs.cam_rule()` reproduces the rule words known to work.

    Checks the encoder against ground truth rather than against itself.
    0x002D2F01 is the word the on-silicon `kr260_eth_xfer.py` writes for the
    proven eth->eth SRAM transfer and 0x00232F01 the one for its mailbox;
    0x002D4001 is what the compute chiplet's own sim uses. 0x002A2F01 is the
    heterogeneous eth->compute mailbox rule — a *different* replace byte for the
    same operation, because the compute mailbox is at 0x2A (its 0x22-0x23 is the
    Cortex-M4 SRAM bit-band alias and is deliberately unmapped).
    Pass: exact word equality, and enable=False clears bit[0] and nothing else.
    """
    assert regs.cam_rule(match, replace) == expect, (
        "cam_rule(0x%02X -> 0x%02X) = 0x%08X, expected 0x%08X"
        % (match, replace, regs.cam_rule(match, replace), expect))
    assert regs.cam_rule(match, replace, enable=False) == expect & ~1


def test_l0_addr_09_cam_rule_field_placement_is_exact():
    """L0-ADDR-09: rule fields sit exactly where `tl_addr_trans_regs.sv` puts
    them — [0] enable, [15:8] match, [23:16] replace, rest reserved-zero.

    A swapped match/replace pair is the easiest way to build a rule that looks
    plausible and silently misroutes every cross-die transaction: 0x2D->0x2F
    instead of 0x2F->0x2D sends peer writes at the far die's *own* peer aperture,
    which is not an inbound target, so it DECERRs — and an unreturned response is
    how this path wedges the PS bus.
    Pass: over a sweep of byte pairs the fields round-trip through
    `decode_cam_rule()`, no reserved bit is ever set, and a non-byte raises.
    """
    for match, replace in itertools.product(
            (0x00, 0x01, 0x23, 0x2A, 0x2D, 0x2F, 0x40, 0x41, 0xFE, 0xFF),
            repeat=2):
        word = regs.cam_rule(match, replace)
        decoded = regs.decode_cam_rule(word)
        assert decoded["enable"] == 1
        assert decoded["match"] == match, (
            "0x%08X: match is 0x%02X, expected 0x%02X (fields swapped?)"
            % (word, decoded["match"], match))
        assert decoded["replace"] == replace, (
            "0x%08X: replace is 0x%02X, expected 0x%02X (fields swapped?)"
            % (word, decoded["replace"], replace))
        assert word & ~H.CAM_RULE_VALID_MASK == 0, (
            "0x%08X sets a reserved bit (valid mask 0x%08X); [7:1] and [31:24] "
            "must be zero" % (word, H.CAM_RULE_VALID_MASK))

    for bad in (0x100, -1, 0x1FF):
        with pytest.raises(ValueError):
            regs.cam_rule(bad, 0x2D)
        with pytest.raises(ValueError):
            regs.cam_rule(0x2F, bad)


def test_l0_addr_10_cam_rule_registers_are_contiguous():
    """L0-ADDR-10: RULE_0..RULE_7 are 4 bytes apart, and there is no rule 8.

    Pins the register-file geometry: one channel, eight rules. Asking for a ninth
    must raise rather than walking into the 0xCAFECAFE reserved gap and writing
    somewhere harmless-looking.
    Pass: exact offset arithmetic for all eight; ValueError for index 8 and -1.
    """
    for index in range(regs.CAM_NUM_RULES):
        assert regs.cam_rule_offset(index) == \
            regs.CAM_RULE_0 + index * regs.CAM_RULE_STRIDE
    for bad in (regs.CAM_NUM_RULES, -1):
        with pytest.raises(ValueError):
            regs.cam_rule_offset(bad)


# ===========================================================================
# Lane-status decode
# ===========================================================================

def test_l0_addr_11_lane_status_decode_is_bit_exact():
    """L0-ADDR-11: FCSM decodes from [19:17], cal_done from [16], and `link_up`
    requires both.

    Getting the FCSM shift wrong is not cosmetic: `link_up` is the gate on every
    peer access, and a false "up" verdict permits a peer write on a dead link,
    which hangs the PS bus. `cal_done` has to be part of the criterion because
    the calibrator only completes once the PEER die has role-locked and is
    training — it is the evidence the far die exists at all.
    Pass: FCSM=4 + cal=1 is up; FCSM=4 without cal is down; every other FCSM is
    down; and lane_locked/lane_fault noise cannot bleed into either field.
    """
    assert regs.FCSM_LINK_IDLE == 4

    up = regs.decode_lane_status((4 << 17) | (1 << 16))
    assert up.fcsm == 4 and up.cal_done == 1 and up.link_up
    assert up.fcsm_name == "LINK_IDLE"

    no_cal = regs.decode_lane_status(4 << 17)
    assert no_cal.fcsm == 4 and no_cal.cal_done == 0
    assert not no_cal.link_up, (
        "link_up must require cal_done as well as FCSM=4 — without it there is "
        "no evidence the far die ever trained")

    for fcsm in range(8):
        status = regs.decode_lane_status((fcsm << 17) | (1 << 16))
        assert status.fcsm == fcsm, (
            "FCSM decoded as %d for raw 0x%08X, expected %d — FCSM is [19:17]"
            % (status.fcsm, (fcsm << 17) | (1 << 16), fcsm))
        assert status.link_up == (fcsm == regs.FCSM_LINK_IDLE)

    noisy = regs.decode_lane_status((1 << 24) | (1 << 23) | (4 << 17)
                                    | (1 << 16) | (0x5A << 8) | 0xA5)
    assert noisy.fcsm == 4 and noisy.cal_done == 1, (
        "lane_locked[7:0] / lane_fault[15:8] corrupted the FCSM/cal decode")
    assert noisy.lane_locked == 0xA5 and noisy.lane_fault == 0x5A
    assert noisy.cr_seen == 1 and noisy.crack_seen == 1


def test_l0_addr_12_sticky_status_decode():
    """L0-ADDR-12: `decode_sticky_status()` isolates the latched faults in [3:1].

    The sticky bits (OVERRUN, UNDERRUN, MASTER_ERROR) never self-clear, so they
    answer "did anything go wrong at any point since reset" — which is what L5's
    health regression asserts on. `packet_committed` [4] and `returner_busy` [0]
    sit either side of them and are *not* faults; folding them into the sticky
    mask would make every busy link look broken.
    Pass: the mask covers exactly [3:1]; each fault bit decodes individually; the
    non-fault neighbours are excluded.
    """
    assert regs.STATUS_STICKY_MASK == 0xE

    clean = regs.decode_sticky_status(0)
    assert clean["sticky"] == 0
    assert not any(clean[k] for k in ("overrun", "underrun", "master_error"))

    for bit, name in ((1, "overrun"), (2, "underrun"), (3, "master_error")):
        decoded = regs.decode_sticky_status(1 << bit)
        assert decoded[name] == 1 and decoded["sticky"] == (1 << bit), (
            "STATUS bit %d should decode as %r alone; got %r" % (bit, name, decoded))

    neighbours = regs.decode_sticky_status((1 << 0) | (1 << 4))
    assert neighbours["sticky"] == 0, (
        "returner_busy[0] and packet_committed[4] are status, not faults — they "
        "must not appear in the sticky mask, or every busy link reads as faulted")
    assert neighbours["packet_committed"] == 1


# ===========================================================================
# Inbound confinement and the heterogeneous mapping
# ===========================================================================

@pytest.mark.parametrize("name", RECEIVERS)
def test_l0_addr_13_inbound_is_confined_to_sram_and_mailbox(name):
    """L0-ADDR-13: every chiplet declares exactly the two inbound regions the RTL
    decodes, and the peer aperture is not one of them.

    The load-bearing fact of the whole design: inbound from the far die reaches
    `shared_sram_0` and `ipc_mailbox_0` and **nothing else** — anything else
    takes a DECERR from the top-level default slave. If a target ever declared a
    third region, L4's confinement test would be asserting the wrong thing.

    The second half matters just as much: the peer aperture must be a genuinely
    *different* byte from both inbound regions. If they were equal, the CAM would
    be an identity no-op and a "passing" cross-die transfer would prove nothing
    about the translation.
    Pass: inbound keys are exactly {shared_sram, ipc_mailbox}; shared_sram is 0x2D
    on every chiplet; the peer aperture collides with neither.
    """
    target = TARGETS[name]
    assert set(target.inbound_targets) == {INBOUND_SHARED_SRAM,
                                           INBOUND_IPC_MAILBOX}, (
        "%s declares inbound targets %s; the RTL decodes exactly shared_sram_0 "
        "and ipc_mailbox_0" % (name, sorted(target.inbound_targets)))
    assert target.inbound_byte(INBOUND_SHARED_SRAM) == 0x2D, (
        "%s: shared_sram_0 is at 0x2D on both the eth and the compute chiplet; "
        "got 0x%02X" % (name, target.inbound_byte(INBOUND_SHARED_SRAM)))
    for key, byte in target.inbound_targets.items():
        assert byte != target.peer_aperture, (
            "%s: inbound %s (0x%02X) equals the peer aperture. The CAM would be "
            "an identity map, and a passing transfer would prove nothing."
            % (name, key, byte))
        assert target.inbound_soc_base(key) == byte << 24


def test_l0_addr_14_the_two_chiplets_disagree_about_the_mailbox_byte():
    """L0-ADDR-14: the eth and compute chiplets put `ipc_mailbox_0` at different
    address bytes.

    This is the single fact that makes the pair heterogeneous in a way that can
    silently break things. Copying the proven eth->eth mailbox rule (0x2F->0x23)
    at a compute die targets 0x23, which that SoC deliberately leaves unmapped
    (0x22-0x23 is the Cortex-M4's SRAM bit-band alias), so the write DECERRs on
    the far side with no local evidence why. The correct rule is 0x2F->0x2A.

    Asserting the *disagreement* explicitly means the day someone "tidies up" the
    two descriptors to match, this test says so.
    Pass: both chiplets agree on shared_sram (0x2D) and disagree on ipc_mailbox
    (0x23 vs 0x2A).
    """
    eth = TARGETS.get("kr260-eth-chiplet")
    compute = TARGETS.get("kr260-compute-chiplet")
    if eth is None or compute is None:
        pytest.skip("both chiplet targets must be registered to compare them")

    assert eth.inbound_byte(INBOUND_SHARED_SRAM) \
        == compute.inbound_byte(INBOUND_SHARED_SRAM) == 0x2D
    assert eth.inbound_byte(INBOUND_IPC_MAILBOX) == 0x23
    assert compute.inbound_byte(INBOUND_IPC_MAILBOX) == 0x2A, (
        "the compute chiplet's ipc_mailbox_0 is at 0x2A "
        "(nanosoc_compute_soc.yaml:989); 0x22-0x23 is its Cortex-M4 bit-band "
        "alias and is deliberately unmapped")
    assert eth.inbound_byte(INBOUND_IPC_MAILBOX) \
        != compute.inbound_byte(INBOUND_IPC_MAILBOX), (
        "the two chiplets now agree on the mailbox byte. If that is a real "
        "design change, the heterogeneous CAM derivation loses its only "
        "asymmetric case and L4's mailbox test stops proving anything about "
        "taking the replace byte from the RECEIVER.")


@pytest.mark.parametrize("src_name,dst_name",
                         list(itertools.product(SENDERS, RECEIVERS)))
@pytest.mark.parametrize("which", [INBOUND_SHARED_SRAM, INBOUND_IPC_MAILBOX])
def test_l0_addr_15_cross_target_cam_rule_lands_correctly(src_name, dst_name,
                                                          which):
    """L0-ADDR-15: for EVERY ordered target pair, the derived rule translates the
    sender's peer aperture onto the receiver's inbound region, preserving
    addr[23:0].

    The heterogeneous test hardware cannot run yet. It is verified against a
    Python model of `tl_addr_trans_cam.sv`: match on addr_norm[31:24], replace
    addr[31:24], pass the RAW addr[23:0] through. The direction matters — a rule
    built from the *sender's* map instead of the receiver's produces 0x2F->0x23
    for an eth->compute mailbox write, which DECERRs.
    Pass: at the aperture's first and last word and at the transfer offset, the
    translated address equals the receiver's inbound address at the same offset;
    with the CAM disabled it is the untranslated identity (which is exactly what
    L4's CAM-off control test relies on).
    """
    src, dst = TARGETS[src_name], TARGETS[dst_name]
    rule = H.cam_rule_between(src, dst, which)
    decoded = regs.decode_cam_rule(rule)
    assert decoded["match"] == src.peer_aperture, "match is the SENDER's aperture"
    assert decoded["replace"] == dst.inbound_byte(which), (
        "replace must be the RECEIVER's %s byte (0x%02X), not the sender's "
        "(0x%02X)" % (which, dst.inbound_byte(which), src.inbound_byte(which)))

    for offset in (0x000000, 0x000004, H.XFER_OFFSET, 0xFFFFFC):
        out = H.translate(rule, src.peer(offset))
        assert out == dst.inbound_soc_base(which) + offset, (
            "%s -> %s (%s): peer 0x%08X translated to 0x%08X, expected 0x%08X"
            % (src_name, dst_name, which, src.peer(offset), out,
               dst.inbound_soc_base(which) + offset))
        assert out & 0xFFFFFF == offset, (
            "the CAM passes addr[23:0] through unchanged; offset 0x%06X became "
            "0x%06X" % (offset, out & 0xFFFFFF))

    peer = src.peer(H.XFER_OFFSET)
    assert H.translate(rule, peer, global_enable=False) == peer, (
        "with global_enable=0 the CAM must pass the address through untouched")


@pytest.mark.parametrize("src_name", SENDERS)
def test_l0_addr_16_base_offset_does_not_shift_the_landing_offset(src_name):
    """L0-ADDR-16: with a non-zero BASE_OFFSET, the low 24 bits still come from
    the RAW address, not the normalised one.

    An asymmetry in `tl_addr_trans_cam.sv` that is easy to model wrongly and
    impossible to see with BASE_OFFSET=0 — which is why it needs its own test.
    On a match, `addr_o[23:0] = addr_i[23:0]` (the raw aperture offset), while on
    a *miss* the identity byte is `addr_norm[31:24]` (base-subtracted). So the
    offset inside the destination region always equals the offset inside the
    aperture, whatever BASE_OFFSET is.

    This matters because BASE_OFFSET is a legitimate way to express the same
    mapping — PEER_APERTURE_PROGRAMMING.md §4 notes that base_offset=0x2F000000
    with match=0x00 is equivalent to base_offset=0 with match=0x2F. If a tool
    modelled the low bits as normalised, that "equivalent" configuration would
    silently shift every cross-die write by the low bits of BASE_OFFSET —
    landing the payload somewhere else inside the far die's SRAM, where a
    poison-and-check test would report "nothing crossed".
    Pass: for a BASE_OFFSET carrying low bits, the translated address is the
    destination region plus the RAW aperture offset.
    """
    src = TARGETS[src_name]
    dst = TARGETS[src_name]
    replace = dst.inbound_byte(INBOUND_SHARED_SRAM)
    base_offset = (src.peer_aperture << 24) | 0x000004
    rule = regs.cam_rule(0x00, replace)      # match the normalised upper byte

    for offset in (0x001000, 0x000010, 0x00FFFC):
        out = H.translate(rule, src.peer(offset), base_offset=base_offset)
        assert out == (replace << 24) | offset, (
            "%s: with BASE_OFFSET=0x%08X, peer 0x%08X translated to 0x%08X; "
            "expected 0x%08X. On a match the CAM takes addr[23:0] from the RAW "
            "address, not from addr - BASE_OFFSET — modelling it the other way "
            "shifts every cross-die write by 0x%X inside the far die's region."
            % (src_name, base_offset, src.peer(offset), out,
               (replace << 24) | offset, base_offset & 0xFFFFFF))


def test_l0_addr_17_no_chiplet_declares_the_tx_aperture_as_its_peer_window():
    """L0-ADDR-16: a peer aperture is the `haddr[24]==1` half of the D2D window —
    never the TX-aperture half.

    `chiplet_d2d_decode.sv` splits the D2D window on `haddr[24]`: 0 selects the
    TX aperture — the no-backpressure path the backlog explicitly warns off — and
    1 selects `hsel_peer` -> `ahb_sub` -> CAM. The eth window is 0x2E so its peer
    aperture is 0x2F (odd). The compute window is 0x4000_0000, which by the same
    decoder puts its peer aperture at **0x41**, not the 0x40 both of that repo's
    G2 testbenches use — and those testbenches bypass `chiplet_d2d_decode`
    entirely, so they cannot see the difference.

    The framework's answer is to declare NO peer aperture for the compute die
    rather than pick one, and this test pins that: an even byte here would mean
    the framework had resolved the ambiguity by guessing, in favour of the
    aperture that wedges.
    Pass: every declared peer aperture is odd, and the compute chiplet declares
    none at all.
    """
    for name in CHIPLETS:
        target = TARGETS[name]
        if target.peer_aperture == NO_PEER_APERTURE:
            continue
        assert target.peer_aperture % 2 == 1, (
            "%s declares peer aperture 0x%02X. In chiplet_d2d_decode.sv the peer "
            "window is haddr[24]==1, i.e. the ODD upper byte of the D2D window "
            "(eth: window 0x2E -> peer 0x2F; compute: window 0x40 -> peer 0x41). "
            "An even byte selects the TX aperture — the no-backpressure path "
            "that wedges." % (name, target.peer_aperture))

    compute = TARGETS.get("kr260-compute-chiplet")
    if compute is not None:
        # Post-G4 (docs/BRINGUP_GAPS.md §G4): the compute D2D window is
        # 0x4000_0000, so haddr[24]==1 puts its peer aperture at 0x41 (link 0) /
        # 0x61 (link 1) — the ODD upper byte, NOT the 0x40/0x60 TX half the two
        # compute G2 sims poke with chiplet_d2d_decode bypassed. Encoded as the
        # post-decoder INTENDED value (SoC-internal only; to_host() stays
        # fail-loud until G2 lands a real bitstream). Confirm 0x41/0x61 with the
        # decoder in path before trusting a cross-die transfer.
        assert compute.peer_aperture == 0x41, (
            "the compute chiplet's link-0 peer aperture should be 0x41, the "
            "haddr[24]==1 half of its 0x4000_0000 D2D window (G4); got 0x%02X"
            % compute.peer_aperture)
        assert compute.peer_aperture % 2 == 1, (
            "the peer aperture must be the odd half of the D2D window, never the "
            "even (TX-aperture) half that wedges")
        link_peers = {row[0]: row[5] for row in compute.d2d_links}
        assert link_peers == {"link0": 0x41, "link1": 0x61}, (
            "compute is dual-link: link 0 -> peer 0x41, link 1 -> peer 0x61; "
            "got %r" % link_peers)


# ===========================================================================
# The no-hardware guarantee
# ===========================================================================

def test_l0_addr_18_l0_imports_no_hardware_backend():
    """L0-ADDR-18: importing the whole `hetsoc` package pulls in no board
    backend, no `pynq`, and opens no `/dev/mem`.

    API contract non-negotiable #4 — hardware access stays behind lazy imports.
    If it regresses, L0 stops being runnable in CI and on a developer laptop,
    and L0 is the one level that must always run.
    Pass: `pynq` is absent from sys.modules and no open file descriptor points at
    /dev/mem, after importing every hetsoc module this suite uses.
    """
    import hetsoc.board          # noqa: F401
    import hetsoc.config         # noqa: F401
    import hetsoc.fpgahub        # noqa: F401
    import hetsoc.health         # noqa: F401
    import hetsoc.pair           # noqa: F401
    import hetsoc.regs           # noqa: F401
    import hetsoc.safety         # noqa: F401
    import hetsoc.targets        # noqa: F401

    hardware_modules = sorted(m for m in sys.modules
                              if m == "pynq" or m.startswith("pynq."))
    assert not hardware_modules, (
        "importing hetsoc pulled in %s. Hardware backends must be lazily "
        "imported so L0 runs with no board and no PYNQ install."
        % hardware_modules)

    fd_dir = "/proc/self/fd"
    if os.path.isdir(fd_dir):
        opened = []
        for fd in os.listdir(fd_dir):
            try:
                opened.append(os.readlink(os.path.join(fd_dir, fd)))
            except OSError:
                continue
        assert "/dev/mem" not in opened, (
            "importing hetsoc opened /dev/mem. There must be no unchecked path "
            "to physical memory.")


# ===========================================================================
# C1 — the PS backdoor that cannot reach the D2D window.
# ===========================================================================
def test_l0_addr_19_d2d_window_refused_when_ps_cannot_reach_it():
    """L0-ADDR-19: a die whose PS port cannot reach D2D must REFUSE those addrs.

    Compute G2 exported `ps_ahb_s`, but `ps_m`'s target list omits d2d0/d2d1
    (nanosoc_compute_soc.yaml:1110). Measured in sim/het_pair on 2026-07-30: the
    port works (write+read-back to compute shared_sram_0 @0x2D002000 returns
    0xa5a5beef) while 0x40032080/2084/2108 all read 0x00000000.

    Why this must be an exception and not a returned address: the hardware
    failure is SILENT. An unreachable read returns zeros, and zeros decode as
    fcsm=0/cal_done=0 — indistinguishable from a down link. A bring-up script
    would report "link failed" and send an operator to check a ribbon that is
    fine. Refusing in code turns a misdiagnosis into a message.
    """
    import dataclasses

    from hetsoc.safety import AddressGuardError
    from hetsoc.targets import get_target

    compute = get_target("kr260-compute-chiplet")
    assert compute.ps_reaches_d2d is False, (
        "the compute descriptor no longer records C1. If d2d0/d2d1 were added "
        "to ps_m's targets, that is good news — update this test and cite the "
        "commit; do not just flip the flag.")

    # The shipped descriptor is unresolved (window_size == 0) so every address
    # already fails. Resolve a copy so the D2D guard is what is under test and
    # not the provisional guard standing in front of it.
    resolved = dataclasses.replace(
        compute, window_size=0x1_0000_0000, provisional=False,
        source=compute.source + " [+ test-local window to exercise the D2D guard]")

    # An address in the INTERNAL map is fine — the backdoor genuinely reaches it.
    assert resolved.to_host(0x2D00_1000) == resolved.window_base + 0x2D00_1000

    # Anything inside either D2D window is refused, with the cause named.
    for soc_addr, why in ((0x4003_2108, "link0 TideLink APB"),
                          (0x4003_4010, "link0 CAM rule 0"),
                          (0x4100_1000, "link0 peer aperture"),
                          (0x6003_2108, "link1 TideLink APB"),
                          (0x6100_1000, "link1 peer aperture")):
        try:
            got = resolved.to_host(soc_addr)
        except AddressGuardError as exc:
            assert "CANNOT REACH" in str(exc), (
                "the refusal for %s (0x%X) does not explain itself: %s"
                % (why, soc_addr, exc))
            assert "0x00000000" in str(exc), (
                "the refusal for %s must warn that the silent hardware failure "
                "reads back zeros and looks like a down link" % why)
        else:
            raise AssertionError(
                "%s (0x%X) was mapped to 0x%X instead of being refused. On a "
                "real board that access reads 0x00000000 and every TideLink "
                "status register looks like a down link." % (why, soc_addr, got))


def test_l0_addr_20_eth_die_still_reaches_its_own_d2d_window():
    """L0-ADDR-20: the C1 guard must not fire on a die that IS wired correctly.

    The eth die's eth_ss_0 window carries the SoC map AND the D2D apertures —
    that is how the proven bring-up works. If the guard caught this one too it
    would break the only path that currently functions."""
    from hetsoc.targets import get_target

    eth = get_target("kr260-eth-chiplet")
    assert eth.ps_reaches_d2d is True
    assert eth.to_host(0x2E03_2108) == eth.window_base + 0x2E03_2108
    assert eth.to_host(0x2F00_1000) == eth.window_base + 0x2F00_1000
