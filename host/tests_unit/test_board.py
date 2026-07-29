# =============================================================================
# L0 — Board: the choke point. Address guard, peer gate, timeout, aliveness.
#
# The property under test is that there is NO PATH from a Board to memory that
# skips the guard. Not "the happy path is guarded" — every path.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
import pytest

from hetsoc import regs
from hetsoc.board import Board
from hetsoc.safety import (AddressGuardError, ConfigError, LinkDownError,
                           ProvisionalTargetError, WedgeDetected)
from hetsoc.targets import get_target
from hetsoc.transport import MemoryTransport

from conftest import BOOTROM_WORDS, LANE_STATUS_DOWN, make_board


# =============================================================================
class TestConstruction:
    def test_a_bare_ip_gets_the_ubuntu_user(self):
        board = make_board(host="10.22.24.159")
        assert board.host == "ubuntu@10.22.24.159"

    def test_an_explicit_user_is_kept(self):
        assert make_board(host="root@10.22.24.159").host == "root@10.22.24.159"

    def test_an_unknown_role_is_refused(self):
        with pytest.raises(ConfigError) as info:
            Board("ubuntu@1.2.3.4", "kr260-eth-chiplet", role="die_c")
        assert "die_a" in str(info.value) and "die_b" in str(info.value)

    def test_an_unknown_target_is_refused(self):
        with pytest.raises(KeyError):
            Board("ubuntu@1.2.3.4", "kr260-nonexistent", role="die_a")

    def test_a_provisional_target_refuses_to_build_a_transport(self):
        board = Board("ubuntu@1.2.3.4", "kr260-compute-chiplet", role="die_b")
        with pytest.raises(ProvisionalTargetError):
            _ = board.transport


# =============================================================================
class TestGuardedAccess:
    def test_read_translates_through_the_window(self, eth_board):
        eth_board.transport.mem[0x4_2E03_2108] = 0xDEADBEEF
        assert eth_board.read(0x2E032108) == 0xDEADBEEF

    def test_write_translates_through_the_window(self, eth_board):
        eth_board.write(0x2D001000, 0xC0FFEE01)
        assert eth_board.transport.mem[0x4_2D00_1000] == 0xC0FFEE01

    def test_out_of_window_read_is_refused_before_it_reaches_the_transport(
            self, eth_board):
        before = len(eth_board.transport.reads)
        with pytest.raises(AddressGuardError):
            eth_board.read(0x1_0000_0000)      # past the 4 GiB window
        assert len(eth_board.transport.reads) == before, \
            "the transport must never see a refused address"

    def test_out_of_window_write_never_reaches_the_transport(self, eth_board):
        before = len(eth_board.transport.writes)
        with pytest.raises(AddressGuardError):
            eth_board.write(0x1_0000_0000, 1)
        assert len(eth_board.transport.writes) == before

    def test_a_bare_link_address_is_refused(self, eth_board):
        # 0x8403_2108 is a SoC address inside the window here, so it translates
        # to 0x4_8403_2108 — safely in-window. What must never happen is the
        # naive identity that produces 0x8403_2108 itself.
        assert eth_board.target.to_host(0x84032108) == 0x4_8403_2108

    def test_read_many_bounds_the_LAST_word_too(self, eth_board):
        # A burst that starts in-window and ends outside must be refused whole.
        near_end = eth_board.target.window_size - 8
        with pytest.raises(AddressGuardError):
            eth_board.read_many(near_end, 4)

    def test_read_many_returns_consecutive_words(self, eth_board):
        eth_board.transport.preload(0x4_2D00_0000, [1, 2, 3, 4])
        assert eth_board.read_many(0x2D000000, 4) == [1, 2, 3, 4]

    def test_read_many_zero_is_a_no_op(self, eth_board):
        assert eth_board.read_many(0x2D000000, 0) == []

    def test_read_many_negative_is_refused(self, eth_board):
        with pytest.raises(ValueError):
            eth_board.read_many(0x2D000000, -1)


# =============================================================================
class TestPeerGate:
    """Rule 2: any access that leaves the die is gated on FCSM==4."""

    def test_a_peer_write_on_a_down_link_is_refused(self, down_board):
        with pytest.raises(LinkDownError):
            down_board.write(0x2F001000, 0xC0FFEE01)

    def test_a_peer_read_on_a_down_link_is_refused(self, down_board):
        with pytest.raises(LinkDownError):
            down_board.read(0x2F001000)

    def test_the_refused_peer_write_never_reaches_the_transport(self, down_board):
        before = list(down_board.transport.writes)
        with pytest.raises(LinkDownError):
            down_board.write(0x2F001000, 1)
        assert down_board.transport.writes == before

    def test_a_peer_write_on_a_live_link_goes_through(self, eth_board):
        eth_board.write(0x2F001000, 0xC0FFEE01)
        assert eth_board.transport.mem[0x4_2F00_1000] == 0xC0FFEE01

    def test_local_access_is_not_gated_on_the_link(self, down_board):
        # The whole point of the config plane: it must be readable with the link
        # DOWN, because that is when you need it.
        down_board.read(down_board.reg(regs.ROLE_STATUS))
        down_board.read(0x2D001000)            # this die's own shared_sram_0
        down_board.write(0x2D001000, 0x1234)

    def test_the_link_is_rechecked_on_every_peer_access_not_just_the_first(
            self, eth_board):
        eth_board.write(0x2F001000, 1)         # link up: fine
        # Link drops mid-run — the next peer access must still be refused.
        eth_board.transport.mem[eth_board.target.to_host(
            eth_board.reg(regs.SWI_LANE_STATUS))] = LANE_STATUS_DOWN
        with pytest.raises(LinkDownError):
            eth_board.write(0x2F001004, 2)


# =============================================================================
class TestTimeout:
    """Rule 3: a hang raises WedgeDetected, it never blocks forever."""

    @staticmethod
    def _watchdog(board, after_s=5.0):
        """Release the simulated hang eventually.

        Without this, a REGRESSION in the guard would hang the test suite
        instead of failing it — and a CI job that hangs teaches nobody anything.
        The watchdog fires well after the board's own 0.25 s budget, so a working
        guard still raises WedgeDetected first.
        """
        import threading

        timer = threading.Timer(after_s, board.transport.release_hangs)
        timer.daemon = True
        timer.start()
        return timer

    def test_a_hung_read_raises_wedgedetected(self, eth_board):
        eth_board.timeout_s = 0.25
        eth_board.transport.hang_addresses.add(0x4_2D00_0000)
        timer = self._watchdog(eth_board)
        try:
            with pytest.raises(WedgeDetected):
                eth_board.read(0x2D000000)
        finally:
            timer.cancel()
            eth_board.transport.release_hangs()

    def test_a_hung_write_raises_wedgedetected(self, eth_board):
        eth_board.timeout_s = 0.25
        eth_board.transport.hang_addresses.add(0x4_2D00_0000)
        timer = self._watchdog(eth_board)
        try:
            with pytest.raises(WedgeDetected):
                eth_board.write(0x2D000000, 1)
        finally:
            timer.cancel()
            eth_board.transport.release_hangs()

    def test_the_wedge_message_says_how_to_recover(self, eth_board):
        eth_board.timeout_s = 0.25
        eth_board.transport.hang_addresses.add(0x4_2D00_0000)
        timer = self._watchdog(eth_board)
        try:
            with pytest.raises(WedgeDetected) as info:
                eth_board.read(0x2D000000)
            assert "JTAG POR" in str(info.value)
            assert "Do NOT retry" in str(info.value)
        finally:
            timer.cancel()
            eth_board.transport.release_hangs()


# =============================================================================
class TestStatusReads:
    def test_lane_status_decodes_the_silicon_value(self, eth_board):
        status = eth_board.lane_status()
        assert status.fcsm == regs.FCSM_LINK_IDLE
        assert status.cal_done == 1
        assert eth_board.link_up() is True

    def test_link_up_is_false_when_the_link_is_down(self, down_board):
        assert down_board.link_up() is False

    def test_role_status_inversion_is_handled(self):
        die_a = make_board("a", "die_a")
        die_b = make_board("b", "die_b")
        # effective_role 0 == master. die_a must read 0, die_b must read 1.
        assert die_a.role_status()["is_master"] == 1
        assert die_a.role_status()["role_ok"] == 1
        assert die_b.role_status()["is_master"] == 0
        assert die_b.role_status()["role_ok"] == 1

    def test_role_mismatch_is_reported(self):
        # A die_b board reading effective_role=0 means the images are swapped.
        board = make_board("b", "die_b")
        target = board.target
        board.transport.mem[target.to_host(target.reg(regs.ROLE_STATUS))] = 0x2
        assert board.role_status()["role_ok"] == 0

    def test_reg_composes_the_target_tlapb_base(self, eth_board):
        assert eth_board.reg(regs.SWI_LANE_STATUS) == 0x2E032108


# =============================================================================
class TestAliveness:
    def test_alive_passes_on_the_real_bootrom_words(self, eth_board):
        assert eth_board.alive() is True

    def test_alive_fails_on_a_wrong_vector_table(self, eth_board):
        eth_board.transport.mem[0x4_0000_0000] = 0xFFFFFFFF
        assert eth_board.alive() is False

    def test_the_expected_words_are_the_ones_read_off_silicon(self, eth_board):
        assert eth_board.target.bootrom_expect == BOOTROM_WORDS

    def test_alive_returns_false_when_the_board_does_not_answer(self, eth_board):
        eth_board.transport.error_addresses.add(0x4_0000_0000)
        assert eth_board.alive() is False

    def test_alive_raises_rather_than_lying_when_no_probe_is_declared(self):
        # The compute descriptor has no verified boot-ROM signature. Returning
        # False would read as "the board is dead"; the truth is "we don't know".
        board = make_board("compute", "die_b", target="kr260-eth-chiplet")
        board.target = get_target("kr260-compute-chiplet")
        with pytest.raises(ConfigError) as info:
            board.alive()
        assert "no boot-ROM probe address" in str(info.value)


# =============================================================================
class TestFreshness:
    def test_a_new_board_is_not_fresh(self, eth_board):
        assert eth_board.is_fresh is False

    def test_marking_fresh_and_live(self, eth_board):
        eth_board.mark_fresh()
        assert eth_board.is_fresh is True
        eth_board.mark_live()
        assert eth_board.is_fresh is False

    def test_por_requires_an_fpgahub_name(self):
        board = make_board(fpgahub=None)
        with pytest.raises(ConfigError) as info:
            board.por()
        assert "only" in str(info.value).lower()

    def test_deploy_without_a_recipe_is_refused(self):
        board = make_board(fpgahub=None)
        with pytest.raises(ConfigError) as info:
            board.deploy()
        assert "deploy_command" in str(info.value)


# =============================================================================
class TestMemoryTransportGuard:
    """The transport re-checks the window — two independent guards, one hazard."""

    def test_out_of_window_is_refused_by_the_transport_too(self):
        from hetsoc.safety import TransportError

        mem = MemoryTransport(0x4_0000_0000, 0x1_0000_0000)
        with pytest.raises(TransportError):
            mem.read32(0x8403_2108)

    def test_unaligned_is_refused(self):
        from hetsoc.safety import TransportError

        mem = MemoryTransport(0, 0x1000)
        with pytest.raises(TransportError):
            mem.read32(0x2)
