# =============================================================================
# L0 — the wedge diagnostic.
#
# The distinction being tested: OBS_FC_CREDIT and SWI_LANE_STATUS observe the
# TideLink SIDEBAND node, which keeps the SoC-Labs recovery logic. The five AXI
# data nodes (AW/W/B/AR/R) ship the recovery-stripped upstream FCSM and are the
# ones that wedge. A verdict that reports "healthy" because the sideband is fine
# while an AXI node's Ack/Nack FIFO is stuck is the exact failure mode that let
# both boards wedge on 2026-07-29.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
from hetsoc import health, regs


def _poke_node(board, node_base, crc=0, acknack=1, txfifo=1):
    target = board.target
    board.transport.mem[target.to_host(target.reg(node_base + regs.FC_CRC))] = crc
    board.transport.mem[target.to_host(target.reg(node_base + regs.FC_ACKNACK))] = acknack
    board.transport.mem[target.to_host(target.reg(node_base + regs.FC_TXFIFO))] = txfifo


# =============================================================================
class TestFcHealth:
    def test_a_clean_link_reports_ok(self, eth_board):
        result = health.fc_health(eth_board)
        assert result["ok"] is True
        assert result["stuck"] == []
        assert result["worst_crc"] == 0
        assert set(result["nodes"]) == {n for n, _b in regs.FC_NODES}

    def test_a_stuck_acknack_fifo_on_an_axi_node_is_flagged(self, eth_board):
        _poke_node(eth_board, 0x1200, acknack=0)     # B node, FIFO NOT empty
        result = health.fc_health(eth_board)
        assert result["stuck"] == ["B"]
        assert result["ok"] is False

    def test_a_non_empty_fifo_on_the_sideband_node_is_not_the_wedge_signature(
            self, eth_board):
        # The TideLink node keeps socl_reack; a busy FIFO there recovers.
        _poke_node(eth_board, 0x1700, acknack=0)
        result = health.fc_health(eth_board)
        assert result["stuck"] == []

    def test_crc_errors_are_surfaced(self, eth_board):
        _poke_node(eth_board, 0x1400, crc=7)         # R node
        result = health.fc_health(eth_board)
        assert result["worst_crc"] == 7
        assert result["nodes"]["R"]["crc"] == 7
        assert result["ok"] is False

    def test_crc_is_masked_to_16_bits(self, eth_board):
        _poke_node(eth_board, 0x1400, crc=0xDEAD_0005)
        assert health.fc_health(eth_board)["nodes"]["R"]["crc"] == 0x0005

    def test_the_axi_nodes_are_labelled(self, eth_board):
        nodes = health.fc_health(eth_board)["nodes"]
        assert nodes["B"]["axi_data"] is True
        assert nodes["TideLink"]["axi_data"] is False


# =============================================================================
class TestLinkHealth:
    def test_a_healthy_sample(self, eth_board):
        sample = health.link_health(eth_board)
        assert sample["link_up"] is True
        assert sample["fcsm"] == 4
        assert sample["sticky"] == 0
        assert sample["verdict"]["ok"] is True
        assert sample["verdict"]["reasons"] == []

    def test_a_down_link_is_a_reason(self, down_board):
        sample = health.link_health(down_board)
        assert sample["verdict"]["ok"] is False
        assert any("link DOWN" in r for r in sample["verdict"]["reasons"])

    def test_sticky_faults_are_a_reason(self, eth_board):
        target = eth_board.target
        eth_board.transport.mem[target.to_host(target.reg(regs.STATUS))] = 0x2
        sample = health.link_health(eth_board)
        assert sample["sticky"] == 0x2
        assert sample["sticky_bits"]["overrun"] == 1
        assert any("sticky fault" in r for r in sample["verdict"]["reasons"])

    def test_the_stuck_axi_node_reason_names_the_recovery_gap(self, eth_board):
        _poke_node(eth_board, 0x1200, acknack=0)
        reasons = health.link_health(eth_board)["verdict"]["reasons"]
        assert any("recovery-stripped" in r for r in reasons)

    def test_a_healthy_sideband_does_not_mask_a_stuck_axi_node(self, eth_board):
        # CREDIT_COUNT = 4096 (idle FIFO) and OBS_FC_CREDIT clean, yet the B node
        # is stalled. This is exactly what the board looked like before it wedged.
        _poke_node(eth_board, 0x1200, acknack=0)
        sample = health.link_health(eth_board)
        assert sample["credit_count"] == 4096
        assert sample["link_up"] is True
        assert sample["verdict"]["ok"] is False, \
            "a clean sideband must NOT produce a healthy verdict"

    def test_the_formatter_marks_the_axi_nodes_and_the_sideband_caveat(
            self, eth_board):
        text = health.format_health(health.link_health(eth_board))
        assert "AXI data" in text
        assert "SIDEBAND" in text
        assert "VERDICT" in text

    def test_health_reads_only(self, eth_board):
        health.link_health(eth_board)
        assert eth_board.transport.writes == []


# =============================================================================
class TestBetweenTransfersPolling:
    def test_a_rising_crc_count_is_the_actionable_signal(self, eth_board):
        first = health.fc_health(eth_board)
        _poke_node(eth_board, 0x1200, crc=1)
        second = health.fc_health(eth_board)
        delta = health.compare_fc_health(first, second)
        assert delta["crc_delta"] == {"B": 1}
        assert delta["degrading"] is True

    def test_a_static_nonzero_count_is_not_degradation(self, eth_board):
        _poke_node(eth_board, 0x1200, crc=5)
        first = health.fc_health(eth_board)
        second = health.fc_health(eth_board)
        assert health.compare_fc_health(first, second)["degrading"] is False

    def test_a_newly_stuck_node_is_degradation(self, eth_board):
        first = health.fc_health(eth_board)
        _poke_node(eth_board, 0x1400, acknack=0)
        second = health.fc_health(eth_board)
        delta = health.compare_fc_health(first, second)
        assert delta["newly_stuck"] == ["R"]
        assert delta["degrading"] is True

    def test_the_first_sample_has_no_delta(self, eth_board):
        result = health.sample_between_transfers(eth_board)
        assert result["delta"] is None
        assert result["degrading"] is False


# =============================================================================
class TestSoakStopsOnDegradation:
    def test_the_soak_stops_before_the_next_transaction_wedges(self,
                                                               homogeneous_pair):
        board = homogeneous_pair.a
        original = health.fc_health

        state = {"calls": 0}

        def degrading(target_board):
            state["calls"] += 1
            result = original(target_board)
            if state["calls"] > 2:
                result["stuck"] = ["B"]        # the credit/ACK stall appears
                result["ok"] = False
            return result

        health.fc_health = degrading
        try:
            result = homogeneous_pair.soak(board, iters=500, sample_every=1)
        finally:
            health.fc_health = original

        assert result["degraded_at"] is not None
        assert result["iters_completed"] < 500
        assert result["ok"] is False
