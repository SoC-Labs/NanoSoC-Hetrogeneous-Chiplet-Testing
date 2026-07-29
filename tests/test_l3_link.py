"""L3 — two boards: link bring-up and the control plane.

The link is brought up **concurrently on both dies** and only converges
bilaterally: each die's `cal_done` asserts once the far board has also
role-locked and its forwarded-clock RX has trained over the ribbon. So a failure
here is almost never "this die is broken" — it is "the peer was not brought up at
the same time, the ribbon is not seated, or the die_a/die_b images are swapped".

Nothing here pushes data across the link: every access is an RO status read or a
config-plane APB write. The one exception is L3-LINK-08, which JTAG-PORs both
boards and is skipped unless `--deploy`.

TideChart tests carry area **TCHART** and are diagnostics rather than silicon
gates: root election and enumeration were never simulated (G-VERIF) and
DEVICE_CLASS is not strapped per die (G1), so a unique root is *likely* but
non-deterministic by construction.

Copyright (C) 2026, SoC Labs (www.soclabs.org)
"""
from __future__ import annotations

import concurrent.futures as cf
import threading
import time

import pytest

from hetsoc import regs

import _helpers as H

pytestmark = [pytest.mark.l3, pytest.mark.hardware, pytest.mark.pair]

RE_BRINGUP_CYCLES = 2      # L3-LINK-08: POR -> redeploy -> bring-up, per cycle


# ===========================================================================
# Link bring-up and verification
# ===========================================================================

def test_l3_link_01_both_dies_reach_link_idle(linked_pair, record_property):
    """L3-LINK-01: both dies converge to FCSM=4 (LINK_IDLE) with cal_done=1.

    Milestone M1, and the precondition for every cross-die test. Asserted on the
    **decoded** register fields rather than on a log line — the existing bench
    runner greps stdout for "RESULT: PASS", which passes just as happily when the
    script never ran.
    Pass: on BOTH dies, FCSM == LINK_IDLE and cal_done == 1.
    """
    for board in linked_pair.boards:
        status = board.lane_status()
        record_property("%s_lane_status" % board.name, "0x%08X" % status.raw)
        assert status.fcsm == regs.FCSM_LINK_IDLE, (
            "%s did not reach LINK_IDLE: %r (want fcsm=%d). cal_done only "
            "asserts once BOTH dies have role-locked and are training, so check "
            "the peer board and the ribbon before suspecting this die."
            % (board.name, status, regs.FCSM_LINK_IDLE))
        assert status.cal_done == 1, (
            "%s: FCSM=%d but cal_done=0 — the link reached data mode without a "
            "completed calibration, which should be impossible: %r"
            % (board.name, status.fcsm, status))
        assert status.link_up


def test_l3_link_02_verify_link_is_non_destructive(linked_pair):
    """L3-LINK-02: `verify_link()` observes the link without disturbing it.

    Proves the one operation the suite is allowed to repeat. Re-running the
    *bring-up* on a live link is the opposite: LL_SWRESET desyncs it and hangs
    the sender's peer writes — the hazard that wedged die_a on 2026-07-29. So
    `verify_link()` must be a pure read, and this pins that it is.
    Pass: `verify_link()` is True, and SWI_LANE_STATUS is bit-identical on both
    dies before and after.
    """
    before = [board.lane_status().raw for board in linked_pair.boards]
    assert linked_pair.verify_link() is True, (
        "verify_link() is False on a pair the linked_pair fixture already "
        "confirmed is up — the two disagree about what 'up' means")
    after = [board.lane_status().raw for board in linked_pair.boards]
    for board, raw0, raw1 in zip(linked_pair.boards, before, after):
        assert raw0 == raw1, (
            "%s: SWI_LANE_STATUS changed 0x%08X -> 0x%08X across verify_link(). "
            "Anything that writes the link control path can desync a live link."
            % (board.name, raw0, raw1))


def test_l3_link_03_exactly_one_master_and_one_slave(linked_pair):
    """L3-LINK-03: the pair resolves to one master and one slave, matching the
    configured roles.

    Proves the pair is a pair. Two masters (or two slaves) is the signature of
    both boards carrying the same image, which presents as "cal_done never
    asserts" — easy to misdiagnose as a ribbon fault, and it also means every
    ribbon lane has two drivers. On a heterogeneous pair this matters more, not
    less: the role strap is the only thing making two different designs
    asymmetric in the right direction.
    Pass: `roles_ok()`, both dies role_locked, and the two effective roles differ.
    """
    assert linked_pair.roles_ok(), (
        "role straps are wrong: %s. die_a must read effective_role 0 (master — "
        "the bit is INVERTED) and die_b 1."
        % {b.name: b.role_status() for b in linked_pair.boards})

    roles = [board.role_status() for board in linked_pair.boards]
    for board, role in zip(linked_pair.boards, roles):
        assert role["role_locked"] == 1, (
            "%s: role_locked is clear on a link that is up. role_lock IS the "
            "link-active indication in this design (link_active = role_locked_o)."
            % board.name)
    assert roles[0]["effective_role"] != roles[1]["effective_role"], (
        "both dies resolved to the same role — the two boards are running the "
        "same image (die_a vs die_b -flip)")


def test_l3_link_04_lanes_are_fault_free(linked_pair, record_property):
    """L3-LINK-04: no lane reports a fault with the link up.

    FCSM=4 says the link-layer state machine reached LINK_IDLE; `lane_fault` says
    whether the SERDES lanes underneath it are healthy. A converged FCSM over a
    faulted lane is exactly the marginal-eye condition that makes the data plane
    intermittent — and since calibration is one-shot, that condition cannot
    correct itself.

    Note what is deliberately NOT asserted: `lane_locked` self-deasserts to 0x00
    after training, so it is not a health signal and gating on it would fail a
    perfectly good link.
    Pass: lane_fault == 0 on both dies. `sync_detected` is recorded as the drift
    indicator to watch across sessions.
    """
    for board in linked_pair.boards:
        sample = board.health()
        record_property("%s_sync_detected" % board.name, sample["sync_detected"])
        assert sample["lane_fault"] == 0, (
            "%s reports lane_fault=0x%02X with the link up: %s. A faulted lane "
            "under a converged FCSM is the marginal-eye signature — expect the "
            "data plane to be intermittent."
            % (board.name, sample["lane_fault"], H.fmt_health(sample)))


def test_l3_link_05_link_layer_exchange_is_evidenced(linked_pair):
    """L3-LINK-05: both dies have seen the CR and CRACK packets.

    Proves the link layer genuinely carries traffic, which FCSM alone does not:
    `link_active` is literally `role_locked_o`, so it says only "a role was
    locked". `cr_seen`/`crack_seen` are the sticky records of the actual
    control-packet handshake — the on-silicon equivalent of the two-SoC sim's
    `link_carries_m2s()` check, which refuses to believe anything crossed until
    the slave has seen the master's CR and CRACK.
    Pass: cr_seen == 1 and crack_seen == 1 on both dies.
    """
    for board in linked_pair.boards:
        status = board.lane_status()
        assert status.cr_seen == 1 and status.crack_seen == 1, (
            "%s: cr_seen=%d crack_seen=%d with the link up (%r). Without both, "
            "FCSM=4 means only that a role was locked — no control packet has "
            "provably crossed the ribbon."
            % (board.name, status.cr_seen, status.crack_seen, status))


def test_l3_link_06_no_sticky_faults_and_credits_available(linked_pair,
                                                           record_property):
    """L3-LINK-06: an idle, converged link carries no sticky fault and has free
    credits.

    The baseline L5's health regression is measured against. STATUS[3:1] latches
    OVERRUN / UNDERRUN / MASTER_ERROR and never self-clears, so a non-zero value
    on an idle link means something already went wrong before this session
    started — and every later "no new faults" assertion would be measured from a
    dirty zero point.
    Pass: sticky == 0 and CREDIT_COUNT > 0 on both dies; no AXI FC node is
    already stuck. Absolute credit values are recorded, not asserted — that is a
    FIFO depth, not a contract.
    """
    for board in linked_pair.boards:
        sample = board.health()
        record_property("%s_credit_count" % board.name, sample["credit_count"])
        record_property("%s_obs_fc_credit" % board.name,
                        "0x%08X" % sample["obs_fc_credit"])
        assert sample["sticky"] == 0, (
            "%s: sticky faults 0x%X already latched on an idle link "
            "(STATUS=0x%08X; %s). %s"
            % (board.name, sample["sticky"], sample["status_raw"],
               sample["sticky_bits"], H.fmt_health(sample)))
        assert sample["credit_count"] > 0, (
            "%s: CREDIT_COUNT=0 on an idle link — no credit is available for the "
            "sideband to make progress: %s" % (board.name, H.fmt_health(sample)))
        assert not sample["fc"]["stuck"], (
            "%s: AXI FC node(s) %s already have a non-empty Ack/Nack FIFO before "
            "any cross-die traffic. That is the credit/ACK-stall signature; the "
            "next transfer is liable to wedge the bus."
            % (board.name, sample["fc"]["stuck"]))


def test_l3_link_07_verify_link_is_stable_under_repetition(linked_pair):
    """L3-LINK-07: repeated verification returns the same decoded state.

    Proves that *observing* the link does not perturb it, over enough repetitions
    that a read-side side effect would show. This is the standing guard against
    the temptation to "just re-run bring-up to be sure": the safe repeat operation
    is verification, and this pins that it really is safe to repeat.
    Pass: five consecutive samples on both dies give identical FCSM and cal_done,
    both at LINK_IDLE.
    """
    samples = {board.name: [] for board in linked_pair.boards}
    for _ in range(5):
        for board in linked_pair.boards:
            status = board.lane_status()
            samples[board.name].append((status.fcsm, status.cal_done))
    for name, seen in samples.items():
        assert len(set(seen)) == 1, (
            "%s: the link state changed while only being read: %s. Verification "
            "must be a pure observation." % (name, seen))
        assert seen[0] == (regs.FCSM_LINK_IDLE, 1), (
            "%s: stable, but not up — (fcsm, cal_done) = %s" % (name, seen[0]))


@pytest.mark.slow
def test_l3_link_08_re_bringup_after_por_is_deterministic(request, pair,
                                                          record_property):
    """L3-LINK-08 (backlog #7): the link re-converges deterministically across
    power cycles.

    Proves the bench is repeatable rather than lucky. `role_lock` clears only on
    `poresetn`, so a genuine teardown means a JTAG POR of BOTH boards — there is
    no software path back to an un-roled die. If every cycle does not reach FCSM=4
    with cal_done, bring-up is a coin flip and every result that depends on it is
    suspect.

    A POR here is also the *correct* place to run a full bring-up: the hazard is
    re-running it on a LIVE link, not on a freshly deployed one.
    Pass: every POR -> redeploy -> bring-up cycle ends with both dies at FCSM=4,
    cal_done=1 and no sticky fault. Leaves the link UP.
    """
    if not request.config.getoption("deploy"):
        pytest.skip(
            "needs --deploy: a POR drops the PL image, so each cycle must "
            "reflash both dies before bringing the link back up. Without the "
            "deploy capability this test would leave the bench dark.")

    for cycle in range(RE_BRINGUP_CYCLES):
        for board in pair.boards:
            board.por()
        pair.bringup(deploy=True)

        assert pair.verify_link(), (
            "cycle %d/%d: the link did not come back up after POR + redeploy"
            % (cycle + 1, RE_BRINGUP_CYCLES))
        for board in pair.boards:
            sample = board.health()
            record_property("cycle%d_%s" % (cycle, board.name),
                            H.fmt_health(sample))
            assert sample["link_up"], (
                "cycle %d/%d: %s failed to re-converge: %s"
                % (cycle + 1, RE_BRINGUP_CYCLES, board.name,
                   H.fmt_health(sample)))
            assert sample["sticky"] == 0, (
                "cycle %d/%d: %s latched sticky faults 0x%X during re-bring-up"
                % (cycle + 1, RE_BRINGUP_CYCLES, board.name, sample["sticky"]))


# ===========================================================================
# TideChart — chiplet identity / routing bootstrap (NOT PTP)
# ===========================================================================

def test_l3_tchart_01_register_plane_is_alive(linked_pair, record_property):
    """L3-TCHART-01: the TideChart register plane answers on both dies.

    Proves the identity/enumeration block is present and decoding, which every
    other TideChart test presumes. This build instantiates `tidechart_shim` with
    NUM_PORTS=1 facing the single D2D link and the APB sliced to 8 bits, so
    PORT_COUNT is the structural fingerprint — and, like the CAM's PIDR, it is
    the thing that catches a wrong per-target base rather than plausible garbage.
    Pass: PORT_COUNT == 1, DEVICE_CLASS != 0, and a legal TC_STATUS decode on
    both dies. DEVICE_CLASS is recorded rather than pinned: this build straps
    0x0001 on both dies, but strapping them *differently* is the documented fix
    for deterministic election (G1) and must not fail this test.
    """
    for board in linked_pair.boards:
        ports = board.read(board.target.tidechart(regs.TC_PORT_COUNT)) & 0x7
        dclass = board.read(board.target.tidechart(regs.TC_DEVICE_CLASS)) & 0xFFFF
        random_id = board.read(board.target.tidechart(regs.TC_RANDOM_ID)) & 0xFFFF
        status = H.decode_tc_status(
            board.read(board.target.tidechart(regs.TC_STATUS)))

        record_property("%s_device_class" % board.name, "0x%04X" % dclass)
        record_property("%s_random_id" % board.name, "0x%04X" % random_id)
        record_property("%s_tc_status" % board.name, "0x%08X" % status["raw"])

        assert ports == 1, (
            "%s: TC_PORT_COUNT=%d, expected 1 (tidechart_shim is instantiated "
            "with NUM_PORTS=1 facing the single D2D link). A different value "
            "means the TideChart APB base for this target (0x%08X) is wrong."
            % (board.name, ports, board.target.tidechart_base))
        assert dclass != 0, (
            "%s: TC_DEVICE_CLASS reads 0 — the register plane is not answering "
            "(or the base 0x%08X is wrong)"
            % (board.name, board.target.tidechart_base))
        assert status["local_id"] <= H.TC_LOCAL_ID_UNASSIGNED


@pytest.mark.nongating
def test_l3_tchart_02_random_ids_differ(linked_pair, record_property):
    """L3-TCHART-02 (T2, non-gating): the two dies hold different random_ids.

    Proves the election has a tie-break at all. DEVICE_CLASS is **not** strapped
    per die in this build (both instantiate the shim with the default 0x0001) and
    the PUF is disabled, so the whole election falls to `random_id`, a
    free-running counter sampled at reset. Equal random_ids means a silent dual
    root — and TC_ERROR[2] (`dual_root`) is never asserted by the hardware, so
    this read is the only way to see it coming.
    Pass (diagnostic): random_id differs between the dies. Non-gating: equality is
    the known G1 gap, fixed only by strapping DEVICE_CLASS per die and rebuilding
    both bitstreams.
    """
    a, b = linked_pair.a, linked_pair.b
    rid_a = a.read(a.target.tidechart(regs.TC_RANDOM_ID)) & 0xFFFF
    rid_b = b.read(b.target.tidechart(regs.TC_RANDOM_ID)) & 0xFFFF
    cls_a = a.read(a.target.tidechart(regs.TC_DEVICE_CLASS)) & 0xFFFF
    cls_b = b.read(b.target.tidechart(regs.TC_DEVICE_CLASS)) & 0xFFFF
    record_property("random_ids", "0x%04X/0x%04X" % (rid_a, rid_b))
    record_property("device_classes", "0x%04X/0x%04X" % (cls_a, cls_b))

    if (cls_a, rid_a) == (cls_b, rid_b):
        pytest.xfail(
            "both dies advertise {device_class=0x%04X, random_id=0x%04X}: the "
            "election has no tie-break and will silently produce a DUAL ROOT "
            "(TC_ERROR[2] is never set by the hardware, so nothing else would "
            "report it). Known gap G1 — DEVICE_CLASS is not strapped per die and "
            "the PUF is disabled; the deterministic fix is a build-time strap "
            "(die_a 0x0001 < die_b 0x0002) on BOTH bitstreams."
            % (cls_a, rid_a))
    assert (cls_a, rid_a) != (cls_b, rid_b)


@pytest.mark.nongating
def test_l3_tchart_03_root_election_yields_exactly_one_root(linked_pair,
                                                            record_property):
    """L3-TCHART-03 (T1, non-gating): a synchronised election elects exactly one
    root.

    Both dies must enter the election window together — it is at most ~1.3 ms,
    far shorter than SSH command skew — so the two `election_start` writes are
    issued from two threads released by a barrier, and the timeout is widened
    first (G-TMO: the 256-cycle default is shorter than a D2D round-trip).
    Pass (diagnostic): election_done on both dies and `is_root` on exactly one.
    Non-gating: root election and enumeration were **never simulated** (G-VERIF),
    so this is the first real test of that logic anywhere, and with DEVICE_CLASS
    unstrapped the outcome is non-deterministic by construction.
    """
    a, b = linked_pair.a, linked_pair.b
    for board in linked_pair.boards:
        board.write(board.target.tidechart(regs.TC_TIMEOUT), H.TC_TIMEOUT_WIDE)

    barrier = threading.Barrier(2)

    def _start(board):
        barrier.wait(timeout=10)
        board.write(board.target.tidechart(regs.TC_CTRL),
                    H.TC_CTRL_ELECTION_START)
        return time.monotonic()

    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        future_a, future_b = pool.submit(_start, a), pool.submit(_start, b)
        skew_s = abs(future_a.result() - future_b.result())
    record_property("election_start_skew_ms", round(skew_s * 1e3, 3))

    states = {}
    for board in linked_pair.boards:
        try:
            raw = H.poll_until(
                lambda bd=board: bd.read(bd.target.tidechart(regs.TC_STATUS)),
                lambda v: v & 1, timeout_s=5.0,
                what="%s TC_STATUS.election_done" % board.name)
        except H.PollTimeout as exc:
            pytest.xfail("%s (the election window is ~1.3 ms; the two starts were "
                         "%.3f ms apart)" % (exc, skew_s * 1e3))
        states[board.name] = H.decode_tc_status(raw)
        record_property("%s_tc_status_after" % board.name, "0x%08X" % raw)

    roots = [name for name, state in states.items() if state["is_root"]]
    if len(roots) != 1:
        pytest.xfail(
            "the election produced %d root(s) (%s) instead of exactly one. Known "
            "gaps: DEVICE_CLASS is not strapped per die (G1), `force_root` "
            "TC_CTRL[2] is decoded but never consumed in RTL (G-FORCE), and "
            "TC_ERROR[2] never flags dual-root (G-DUALROOT). Diagnose from "
            "TC_RANDOM_ID — see L3-TCHART-02."
            % (len(roots), ", ".join(roots) or "none"))
    assert len(roots) == 1


@pytest.mark.nongating
def test_l3_tchart_04_reset_clears_enumeration_state(linked_pair):
    """L3-TCHART-04 (N3, non-gating): TC_CTRL.reset returns a die to
    unenumerated.

    Proves the escape hatch the TideChart runbook leans on — "reset and retry
    liberally" is the documented response to a failed election, so reset has to
    actually clear state. Leaves both dies reset, which is the correct state to
    start a fresh election from.
    Pass (diagnostic): local_id reads 0x1F (unassigned) and election_done /
    enum_done are clear afterwards. Non-gating: reset not clearing state is a
    listed TideChart RTL gap.
    """
    for board in linked_pair.boards:
        board.write(board.target.tidechart(regs.TC_CTRL), H.TC_CTRL_RESET)

    stale = {}
    for board in linked_pair.boards:
        state = H.decode_tc_status(
            board.read(board.target.tidechart(regs.TC_STATUS)))
        if (state["local_id"] != H.TC_LOCAL_ID_UNASSIGNED
                or state["election_done"] or state["enum_done"]):
            stale[board.name] = state
    if stale:
        pytest.xfail(
            "TC_CTRL.reset did not clear TideChart state: %s (expected "
            "local_id=0x%02X, election_done=0, enum_done=0). Known TideChart RTL "
            "gap — reset does not clear."
            % ({k: "local_id=%d election_done=%d enum_done=%d"
                % (v["local_id"], v["election_done"], v["enum_done"])
                for k, v in stale.items()}, H.TC_LOCAL_ID_UNASSIGNED))
    assert not stale
