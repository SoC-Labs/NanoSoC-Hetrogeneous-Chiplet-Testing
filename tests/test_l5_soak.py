"""L5 — soak, stress and characterisation of the cross-die link.

    *********************************************************************
    *  ATTENDED ONLY — see the banner in test_l4_dataplane.py.          *
    *  Sustained cross-die traffic is the condition most likely to hit  *
    *  the recovery-stripped AXI FC nodes and wedge a board.            *
    *********************************************************************

The soak is **write-only**. Peer writes are the reliable direction and one that
stalls still returns via HREADY; the per-beat peer READ is the wedge-prone
access and lives in L4-DATA-06 behind `--allow-peer-read`. Integrity is checked
once at the end by LOCAL reads on the receiver — wedge-safe — while the soak's
own job is *health under load*: FCSM held, no sticky fault, no CRC growth, no
stalled Ack/Nack FIFO.

All six tests share ONE soak run (`soak_run`). Six separate soaks would be six
times the wedge exposure for no extra coverage; each test asserts a different
property of the same run.

Copyright (C) 2026, SoC Labs (www.soclabs.org)
"""
from __future__ import annotations

import time

import pytest

from hetsoc import health as hetsoc_health
from hetsoc import regs

import _helpers as H

pytestmark = [pytest.mark.l5, pytest.mark.hardware, pytest.mark.pair,
              pytest.mark.data_plane, pytest.mark.soak, pytest.mark.slow]

BASE_PAYLOAD = 0x51A00000
SAMPLE_EVERY = 50


@pytest.fixture(scope="session")
def soak_run(linked_pair, request):
    """Drive N peer-write beats over a 16-word window and record health
    throughout. Session-scoped: one soak, six assertions.

    Poisons the receiver's window first and reads it back locally afterwards, so
    the integrity verdict never traverses the link. `stop_on_degrade` is left on:
    that is fix #2 of the wedge root-cause fix path (the host interim that needs
    no rebuild) — stop on a rising CRC or a stuck Ack/Nack FIFO rather than let
    the next transaction wedge the bus.
    """
    iters = request.config.getoption("soak_iters")
    a, b = linked_pair.a, linked_pair.b
    window = H.XFER_WINDOW_WORDS
    offset = H.XFER_OFFSET
    assert offset + 4 * window <= H.SHARED_SRAM_SIZE

    landed_base = b.target.inbound_soc_base("shared_sram") + offset
    for i in range(window):
        b.write(landed_base + 4 * i, H.POISON + i)

    fc_before = {board.name: hetsoc_health.fc_health(board)
                 for board in linked_pair.boards}

    started = time.monotonic()
    result = linked_pair.soak(a, iters, payload=BASE_PAYLOAD,
                              which="shared_sram", offset=offset,
                              window_words=window, sample_every=SAMPLE_EVERY,
                              stop_on_degrade=True)
    elapsed = time.monotonic() - started

    completed = result["iters_completed"]
    # The window is cycled, so each slot ends up holding the LAST beat that
    # targeted it. A slot holding an older payload lost its final beat.
    expected = {}
    for index in range(completed):
        expected[index % window] = (BASE_PAYLOAD + index) & 0xFFFFFFFF

    if completed:
        last_slot = (completed - 1) % window
        try:
            H.poll_until(lambda: b.read(landed_base + 4 * last_slot),
                         lambda v: v == expected[last_slot], timeout_s=5.0,
                         what="the last soak beat to drain into the receiver's "
                              "shared_sram_0")
        except H.PollTimeout as exc:
            # Not fatal here — L5-SOAK-02 owns the integrity verdict and gives a
            # far better diagnosis than a fixture error would.
            print("hetsoc: soak drain poll: %s" % exc)

    final = b.read_many(landed_base, window)
    fc_after = {board.name: hetsoc_health.fc_health(board)
                for board in linked_pair.boards}
    for board in linked_pair.boards:
        board.reg_write(regs.CAM_CTRL, 0)

    return {
        "result": result,
        "iters": iters,
        "completed": completed,
        "window": window,
        "elapsed_s": elapsed,
        "expected": expected,
        "final": final,
        "sender": a,
        "receiver": b,
        "fc_before": fc_before,
        "fc_after": fc_after,
        "health_after": {board.name: board.health()
                         for board in linked_pair.boards},
    }


def test_l5_soak_01_link_stays_in_data_mode(soak_run, record_property):
    """L5-SOAK-01 (backlog #3): the link stays at FCSM=4 with cal_done for the
    whole soak, and every requested beat completes.

    Proves the link survives sustained load rather than just a first beat. That
    matters more here than on a normal link: calibration is one-shot
    (`calibrated_once_q` permanently gates off re-trigger, and only
    `SWI_FORCE_RECAL` — which the FSM never drives — could re-cal), so the
    sampling point is frozen at bring-up and every additional beat is another
    chance for thermal or jitter drift to sample a bit wrong.
    Pass: FCSM never dipped below LINK_IDLE, cal_done held on every sample, and
    the soak was not cut short by degrading FC health.
    """
    result = soak_run["result"]
    record_property("soak_iters_requested", result["iters_requested"])
    record_property("soak_iters_completed", result["iters_completed"])
    record_property("soak_fcsm_min", result["fcsm_min"])

    assert result["degraded_at"] is None, (
        "the soak stopped early at beat %d because FC health was degrading "
        "(rising CRC or a newly stuck Ack/Nack FIFO). That is the host-side "
        "backstop doing its job — the next transaction would likely have wedged "
        "the bus. Inspect L5-SOAK-04's deltas."
        % result["degraded_at"])
    assert result["iters_completed"] == result["iters_requested"], (
        "only %d of %d beats completed"
        % (result["iters_completed"], result["iters_requested"]))
    assert result["fcsm_min"] == regs.FCSM_LINK_IDLE, (
        "FCSM dipped to %d during the soak (want %d throughout). The link left "
        "data mode under load — that is the recovery-stripped AXI FC node giving "
        "up, the state immediately before a wedge."
        % (result["fcsm_min"], regs.FCSM_LINK_IDLE))
    assert result["cal_all"] == 1, "cal_done dropped during the soak"


def test_l5_soak_02_payload_integrity_over_the_window(soak_run):
    """L5-SOAK-02: every word of the cycled window holds the last value written
    to it.

    The integrity verdict, taken wedge-safely: the receiver reads its OWN SRAM,
    so the check itself never traverses the link. Because the window is cycled,
    the expected value at each slot is the most recent beat that targeted it — a
    beat lost anywhere in the run leaves a stale earlier payload, and a beat that
    never crossed leaves the poison.
    Pass: every slot the soak reached matches its expected final value exactly.
    """
    final = soak_run["final"]
    expected = soak_run["expected"]
    receiver = soak_run["receiver"]

    mismatches = {slot: (final[slot], want) for slot, want in expected.items()
                  if final[slot] != want}
    assert not mismatches, (
        "%s: %d of %d words wrong after %d beats: %s. A slot holding 0x%08X+i "
        "never received anything; one holding an older payload lost its last "
        "beat; zero is the peer-write data-phase drop."
        % (receiver.name, len(mismatches), len(expected), soak_run["completed"],
           {slot: "got 0x%08X want 0x%08X" % pair_
            for slot, pair_ in mismatches.items()}, H.POISON))


def test_l5_soak_03_no_sticky_faults_latched(soak_run, record_property):
    """L5-SOAK-03: no sticky fault latches during or after the soak.

    STATUS[3:1] latches OVERRUN / UNDERRUN / MASTER_ERROR and never self-clears,
    so this answers "did anything go wrong at *any* moment", not "is anything
    wrong now". Checked from the soak's own in-run sampling as well as after,
    because a fault noticed only at the end gives no idea when it happened.
    Pass: `sticky_seen` is 0 across the run, and both dies are clean afterwards —
    including lane_fault, the marginal-eye indicator.
    """
    result = soak_run["result"]
    record_property("soak_sticky_seen", "0x%X" % result["sticky_seen"])

    assert result["sticky_seen"] == 0, (
        "sticky faults 0x%X latched during the soak ([1]OVERRUN [2]UNDERRUN "
        "[3]MASTER_ERROR). They never self-clear, so this happened at some point "
        "in the run even though the link may look fine now."
        % result["sticky_seen"])

    for name, sample in soak_run["health_after"].items():
        assert sample["sticky"] == 0, (
            "%s latched sticky faults 0x%X by the end of the soak: %s"
            % (name, sample["sticky"], H.fmt_health(sample)))
        assert sample["lane_fault"] == 0, (
            "%s reports lane_fault=0x%02X after the soak: %s"
            % (name, sample["lane_fault"], H.fmt_health(sample)))


def test_l5_soak_04_axi_fc_nodes_are_clean(soak_run, record_property):
    """L5-SOAK-04: no CRC growth and no stalled Ack/Nack FIFO on the AXI data
    nodes.

    The diagnostic the wedge root-cause analysis asks for, and it looks where
    nothing else can: `OBS_FC_CREDIT` and `SWI_LANE_STATUS[31:17]` observe the
    TideLink **sideband** node (FCSM_6) only — the one node that kept its
    recovery logic. The five AXI data nodes (AW/W/B/AR/R), the recovery-stripped
    ones that actually wedge, are visible only through their own per-node
    registers.

    A rising CRC count means a bit error got through (calibration drift /
    marginal eye). A non-empty Ack/Nack FIFO on an AXI node is the credit/ACK
    stall signature: with no `socl_reack` backstop on this silicon, `fe_rx_ptr`
    never advances, the ring fills, `fe_rx_is_full` latches, and the wedge
    becomes permanent.
    Pass: zero CRC increase on every node of both dies, and no AXI node left with
    a non-empty Ack/Nack FIFO.
    """
    for name in soak_run["fc_before"]:
        delta = hetsoc_health.compare_fc_health(soak_run["fc_before"][name],
                                                soak_run["fc_after"][name])
        record_property("%s_crc_delta" % name, delta["crc_delta"])
        record_property("%s_newly_stuck" % name, delta["newly_stuck"])

        assert not delta["crc_delta"], (
            "%s: CRC error counts rose during the soak: %s. A bit error reached "
            "an FC node; on the five AXI data nodes there is no recovery path on "
            "this silicon, so this is the precursor to a wedge rather than a "
            "benign retry." % (name, delta["crc_delta"]))
        stuck = soak_run["fc_after"][name]["stuck"]
        assert not stuck, (
            "%s: AXI FC node(s) %s left with a NON-EMPTY Ack/Nack FIFO after the "
            "soak. That is the credit/ACK-stall signature — the node has stopped "
            "draining and the next transfer is liable to hang the PS bus. POR "
            "before continuing." % (name, stuck))


def test_l5_soak_05_sideband_credits_are_not_consumed_by_peer_traffic(
        soak_run, record_property):
    """L5-SOAK-05: `CREDIT_COUNT` does not move under peer-window traffic.

    Pins a fact that is easy to get backwards. The obvious expectation — "credit
    counters should move and recover under load" — is wrong for this path: a
    peer-window transfer rides the **AXI transport** (ahb_sub -> CAM -> XHB500 ->
    Wlink AXI FC nodes -> PHY), not the TideLink FIFO/returner sideband that
    `CREDIT_COUNT` / `RELEASE_THRESHOLD` / `OBS_FC_CREDIT` belong to. A soak that
    leaves CREDIT_COUNT pinned at its idle value is the *evidence* for that
    separation — and it is why tuning `RELEASE_THRESHOLD` cannot affect the
    cross-die wedge.

    Pinning it means a future build that DOES route peer traffic over the
    sideband is caught here, rather than silently invalidating the wedge
    analysis that the whole L4/L5 opt-in policy rests on.
    Pass: the observed CREDIT_COUNT range is a single non-zero value.
    """
    low, high = soak_run["result"]["credit_range"]
    record_property("soak_credit_range", (low, high))

    if low > high:
        pytest.skip(
            "the soak took no credit samples (it ran %d beats, sampling every "
            "%d). Raise --soak-iters to at least %d for this observation to mean "
            "anything." % (soak_run["completed"], SAMPLE_EVERY, SAMPLE_EVERY + 1))
    assert low > 0, (
        "CREDIT_COUNT reached 0 during the soak (range %d..%d) — the sideband is "
        "starved, which it should not be while the peer window bypasses it "
        "entirely." % (low, high))
    assert low == high, (
        "CREDIT_COUNT moved during the soak (range %d..%d). On this design the "
        "peer window rides the AXI transport, NOT the sideband those counters "
        "observe, so it should stay pinned at its idle value. If it now moves, "
        "the data path has changed and docs/CROSS_DIE_WEDGE_ROOTCAUSE.md's 'the "
        "two paths' analysis — including the conclusion that RELEASE_THRESHOLD "
        "tuning cannot affect the wedge — needs revisiting." % (low, high))


def test_l5_soak_06_throughput_characterisation(soak_run, record_property):
    """L5-SOAK-06: record cross-die write throughput and per-beat latency.

    Characterisation, not a performance gate. The numbers are dominated by the
    host's per-beat round-trip to the board, so they measure the whole PS-side
    path rather than the link itself; publishing them as a gate would just make
    the suite flaky on a busy dev host. The assertion is a liveness floor only —
    a rate collapsing toward zero means beats are stalling on HREADY, which is
    the condition immediately before a wedge.
    Pass: the run completed and sustained more than 1 beat/s. Beats/s and mean
    per-beat latency are recorded for the bench log.
    """
    completed = soak_run["completed"]
    elapsed = soak_run["elapsed_s"]
    assert elapsed > 0 and completed > 0, "the soak recorded no beats"

    beats_per_s = completed / elapsed
    mean_ms = (elapsed / completed) * 1e3
    record_property("soak_beats_per_s", round(beats_per_s, 1))
    record_property("soak_mean_beat_ms", round(mean_ms, 3))
    record_property("soak_elapsed_s", round(elapsed, 2))

    assert beats_per_s > 1.0, (
        "cross-die write throughput collapsed to %.2f beats/s (%d beats in "
        "%.1fs, %.1f ms/beat). Beats are stalling on HREADY — the condition "
        "immediately before the PS AXI bus wedges."
        % (beats_per_s, completed, elapsed, mean_ms))
