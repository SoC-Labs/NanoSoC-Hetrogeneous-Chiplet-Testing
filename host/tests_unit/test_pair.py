# =============================================================================
# L0 — ChipletPair: the bring-up refusal, CAM programming, and cross-die aiming.
#
# TWO REAL INCIDENTS ARE ENCODED HERE:
#   * 2026-07-29 — re-running the bring-up (LL_SWRESET) on an already-live link
#     desynced it and hung the sender. `bringup()` must refuse unless the dies
#     are fresh.
#   * the heterogeneous mailbox byte — 0x23 on the eth die, 0x2A on the compute
#     die. A rule built from the sender's map aims at a region the receiver does
#     not decode, which DECERRs and stalls the cross-die path.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
import pytest

from hetsoc import regs
from hetsoc.pair import ChipletPair
from hetsoc.safety import AddressGuardError, ConfigError, LinkDownError
from hetsoc.targets import get_target

from conftest import LANE_STATUS_DOWN, make_board


def _set_lane_status(board, raw):
    board.transport.mem[board.target.to_host(
        board.reg(regs.SWI_LANE_STATUS))] = raw


@pytest.fixture
def down_pair():
    a = make_board("eth_a", "die_a", link_up=False, host="ubuntu@10.22.24.159")
    b = make_board("eth_b", "die_b", link_up=False, host="ubuntu@10.22.24.153")
    return ChipletPair(a, b)


# =============================================================================
class TestConstruction:
    def test_two_boards_with_the_same_role_are_refused(self):
        # Same role means the same image on both boards, which drives two
        # outputs onto every J21 ribbon lane.
        a = make_board("a", "die_a", host="ubuntu@10.22.24.159")
        b = make_board("b", "die_a", host="ubuntu@10.22.24.153")
        with pytest.raises(ConfigError) as info:
            ChipletPair(a, b)
        assert "ribbon lane" in str(info.value)

    def test_the_same_board_twice_is_refused(self):
        board = make_board("a", "die_a")
        with pytest.raises(ConfigError):
            ChipletPair(board, board)

    def test_two_boards_on_the_same_host_are_refused(self):
        a = make_board("a", "die_a", host="ubuntu@10.22.24.159")
        b = make_board("b", "die_b", host="ubuntu@10.22.24.159")
        with pytest.raises(ConfigError):
            ChipletPair(a, b)

    def test_other_returns_the_far_die(self, homogeneous_pair):
        assert homogeneous_pair.other(homogeneous_pair.a) is homogeneous_pair.b
        assert homogeneous_pair.other(homogeneous_pair.b) is homogeneous_pair.a

    def test_other_refuses_a_stranger(self, homogeneous_pair):
        with pytest.raises(ConfigError):
            homogeneous_pair.other(make_board("stranger", "die_a"))

    def test_homogeneous_and_heterogeneous_are_distinguished(self, homogeneous_pair):
        assert homogeneous_pair.heterogeneous is False
        het = ChipletPair(
            make_board("eth", "die_a", host="ubuntu@10.22.24.159"),
            make_board("cmp", "die_b", target="kr260-eth-chiplet",
                       host="ubuntu@10.22.24.153"))
        het.b.target = get_target("kr260-compute-chiplet")
        assert het.heterogeneous is True


# =============================================================================
class TestBringupRefusal:
    def test_refuses_when_the_dies_are_not_fresh(self, homogeneous_pair):
        with pytest.raises(RuntimeError) as info:
            homogeneous_pair.bringup()
        message = str(info.value)
        assert "not FRESH" in message
        assert "desyncs it" in message
        assert "verify_link()" in message

    def test_the_refusal_names_the_live_link_explicitly(self, homogeneous_pair):
        with pytest.raises(RuntimeError) as info:
            homogeneous_pair.bringup()
        assert "CURRENTLY UP" in str(info.value)

    def test_refuses_even_when_the_link_is_down_unless_fresh_or_forced(
            self, down_pair):
        # The task rule is literal: fresh, or an explicit force=.
        with pytest.raises(RuntimeError) as info:
            down_pair.bringup()
        assert "not FRESH" in str(info.value)

    def test_only_one_fresh_die_is_still_a_refusal(self, homogeneous_pair):
        homogeneous_pair.a.mark_fresh()
        with pytest.raises(RuntimeError):
            homogeneous_pair.bringup()

    def test_fresh_dies_are_allowed_to_bring_up(self, down_pair):
        for board in down_pair.boards:
            board.mark_fresh()
        # The fake link never converges, so this fails at the FCSM check — not
        # at the freshness gate, which is what we are asserting.
        with pytest.raises(LinkDownError) as info:
            down_pair.bringup(cal_timeout_s=0.05, converge_timeout_s=0.05)
        assert "did NOT converge" in str(info.value)

    def test_force_bypasses_the_gate(self, down_pair):
        with pytest.raises(LinkDownError):
            down_pair.bringup(force=True, cal_timeout_s=0.05,
                              converge_timeout_s=0.05)

    def test_bringup_writes_role_cfg_with_the_right_lock_value(self, down_pair):
        for board in down_pair.boards:
            board.mark_fresh()
        with pytest.raises(LinkDownError):
            down_pair.bringup(force=True, cal_timeout_s=0.02,
                              converge_timeout_s=0.02)
        for board, want in ((down_pair.a, regs.ROLE_CFG_MASTER_LOCK),
                            (down_pair.b, regs.ROLE_CFG_SLAVE_LOCK)):
            host_addr = board.target.to_host(board.reg(regs.ROLE_CFG))
            written = [v for addr, v in board.transport.writes if addr == host_addr]
            assert written == [want], "%s should lock role as 0x%02X" % (
                board.name, want)

    def test_bringup_marks_the_dies_no_longer_fresh(self, down_pair):
        for board in down_pair.boards:
            board.mark_fresh()
        with pytest.raises(LinkDownError):
            down_pair.bringup(force=True, cal_timeout_s=0.02,
                              converge_timeout_s=0.02)
        assert not any(b.is_fresh for b in down_pair.boards)


# =============================================================================
class TestVerifyLink:
    def test_true_when_both_dies_report_up(self, homogeneous_pair):
        assert homogeneous_pair.verify_link() is True

    def test_false_when_either_die_is_down(self, homogeneous_pair):
        _set_lane_status(homogeneous_pair.b, LANE_STATUS_DOWN)
        assert homogeneous_pair.verify_link() is False

    def test_verify_link_writes_nothing(self, homogeneous_pair):
        homogeneous_pair.verify_link()
        for board in homogeneous_pair.boards:
            assert board.transport.writes == [], "verify_link must be read-only"

    def test_require_link_raises_when_a_die_is_down(self, homogeneous_pair):
        _set_lane_status(homogeneous_pair.a, LANE_STATUS_DOWN)
        with pytest.raises(LinkDownError):
            homogeneous_pair.require_link()

    def test_roles_ok_checks_both_straps(self, homogeneous_pair):
        assert homogeneous_pair.roles_ok() is True
        board = homogeneous_pair.b
        board.transport.mem[board.target.to_host(
            board.reg(regs.ROLE_STATUS))] = 0x2       # reads master on die_b
        assert homogeneous_pair.roles_ok() is False


# =============================================================================
class TestProgramCam:
    def test_arms_ctrl_last_so_a_half_rule_is_never_live(self, homogeneous_pair):
        board = homogeneous_pair.a
        homogeneous_pair.program_cam(board, 0x2F, 0x2D)
        order = [addr for addr, _v in board.transport.writes]
        base = board.target.to_host(board.reg(regs.CAM_BASE))
        rule = board.target.to_host(board.reg(regs.CAM_RULE_0))
        ctrl = board.target.to_host(board.reg(regs.CAM_CTRL))
        assert order == [base, rule, ctrl]

    def test_writes_the_proven_rule_word(self, homogeneous_pair):
        board = homogeneous_pair.a
        homogeneous_pair.program_cam(board, 0x2F, 0x2D)
        rule_addr = board.target.to_host(board.reg(regs.CAM_RULE_0))
        assert board.transport.mem[rule_addr] == 0x002D2F01

    def test_base_offset_is_zeroed_first(self, homogeneous_pair):
        board = homogeneous_pair.a
        homogeneous_pair.program_cam(board, 0x2F, 0x2D)
        base_addr = board.target.to_host(board.reg(regs.CAM_BASE))
        assert board.transport.mem[base_addr] == 0

    def test_a_wrong_match_byte_is_refused(self, homogeneous_pair):
        with pytest.raises(AddressGuardError) as info:
            homogeneous_pair.program_cam(homogeneous_pair.a, 0x2E, 0x2D)
        assert "peer aperture" in str(info.value)

    @pytest.mark.parametrize("replace", [0x2C, 0x20, 0x00, 0x2E])
    def test_a_replace_byte_the_far_die_cannot_decode_is_refused(
            self, homogeneous_pair, replace):
        with pytest.raises(AddressGuardError) as info:
            homogeneous_pair.program_cam(homogeneous_pair.a, 0x2F, replace)
        assert "EXACTLY" in str(info.value)
        assert "wedges" in str(info.value)

    def test_the_negative_test_escape_hatch_exists_and_is_explicit(
            self, homogeneous_pair):
        # CROSS_DIE_TEST_BACKLOG.md item 6: prove inbound confinement holds.
        homogeneous_pair.program_cam(homogeneous_pair.a, 0x2F, 0x2C,
                                     allow_unmapped=True)

    def test_map_peer_to_builds_both_bytes_from_the_descriptors(
            self, homogeneous_pair):
        assert homogeneous_pair.map_peer_to(homogeneous_pair.a,
                                            "shared_sram") == 0x002D2F01
        assert homogeneous_pair.map_peer_to(homogeneous_pair.a,
                                            "ipc_mailbox") == 0x00232F01

    def test_map_peer_to_refuses_a_third_region(self, homogeneous_pair):
        with pytest.raises(AddressGuardError):
            homogeneous_pair.map_peer_to(homogeneous_pair.a, "qspi_flash_0")

    def test_a_heterogeneous_mailbox_rule_uses_the_receivers_byte(self):
        # THE regression this repo exists to prevent: 0x2A, not 0x23.
        eth = make_board("eth", "die_a", host="ubuntu@10.22.24.159")
        compute = make_board("cmp", "die_b", host="ubuntu@10.22.24.153")
        compute.target = get_target("kr260-compute-chiplet")
        pair = ChipletPair(eth, compute)
        rule = pair.map_peer_to(eth, "ipc_mailbox")
        assert rule == 0x002A2F01
        assert rule != 0x00232F01, "the eth-die byte would DECERR on compute"


# =============================================================================
class TestCrossDieAiming:
    def test_peer_write_refuses_an_address_that_would_not_cross(
            self, homogeneous_pair):
        with pytest.raises(AddressGuardError) as info:
            homogeneous_pair.peer_write(homogeneous_pair.a, 0x2D001000, 1)
        assert "would NOT cross the link" in str(info.value)

    def test_peer_write_goes_through_on_a_live_link(self, homogeneous_pair):
        addr = homogeneous_pair.a.target.peer(0x1000)
        homogeneous_pair.peer_write(homogeneous_pair.a, addr, 0xC0FFEE01)
        host_addr = homogeneous_pair.a.target.to_host(addr)
        assert homogeneous_pair.a.transport.mem[host_addr] == 0xC0FFEE01

    def test_peer_write_is_refused_on_a_down_link(self, homogeneous_pair):
        _set_lane_status(homogeneous_pair.a, LANE_STATUS_DOWN)
        with pytest.raises(LinkDownError):
            homogeneous_pair.peer_write(homogeneous_pair.a,
                                        homogeneous_pair.a.target.peer(0x1000), 1)

    def test_cross_die_write_returns_the_far_die_landing_address(
            self, homogeneous_pair):
        landed = homogeneous_pair.cross_die_write(
            homogeneous_pair.a, "shared_sram", 0x1000, 0xC0FFEE01)
        # The proven pairing: peer 0x2F001000 -> far die 0x2D001000.
        assert landed == 0x2D001000

    def test_read_landed_reads_the_receiving_dies_own_memory(self,
                                                             homogeneous_pair):
        board = homogeneous_pair.b
        board.transport.mem[board.target.to_host(0x2D001000)] = 0xC0FFEE01
        assert homogeneous_pair.read_landed(board, "shared_sram", 0x1000) \
            == 0xC0FFEE01

    def test_read_landed_never_needs_the_link(self, homogeneous_pair):
        _set_lane_status(homogeneous_pair.b, LANE_STATUS_DOWN)
        homogeneous_pair.read_landed(homogeneous_pair.b, "shared_sram", 0x1000)


# =============================================================================
class TestMailbox:
    def test_msg_valid_is_written_after_the_data_words(self, homogeneous_pair):
        board = homogeneous_pair.a
        homogeneous_pair.mailbox_send(board, [1, 2, 3, 4])
        peer_ctrl = board.target.to_host(
            board.target.peer(regs.IPC_SLOT0_CTRL))
        peer_last = board.target.to_host(
            board.target.peer(regs.IPC_SLOT0_DATA + 12))
        addrs = [a for a, _v in board.transport.writes]
        assert addrs.index(peer_ctrl) > addrs.index(peer_last), \
            "MSG_VALID must be last or the receiver sees stale data"

    def test_too_many_words_is_refused(self, homogeneous_pair):
        with pytest.raises(ValueError):
            homogeneous_pair.mailbox_send(homogeneous_pair.a, [1, 2, 3, 4, 5])

    def test_mailbox_recv_reads_the_local_mailbox(self, homogeneous_pair):
        board = homogeneous_pair.b
        base = board.target.inbound_soc_base("ipc_mailbox")
        board.transport.preload(board.target.to_host(base), [11, 22, 33, 44])
        board.transport.mem[board.target.to_host(base + regs.IPC_SLOT0_CTRL)] = \
            regs.IPC_MSG_VALID
        board.transport.mem[board.target.to_host(base + regs.IPC_IRQ_STATUS)] = 1
        got = homogeneous_pair.mailbox_recv(board)
        assert got["words"] == [11, 22, 33, 44]
        assert got["msg_valid"] == 1
        assert got["irq_latched"] == 1
        assert got["base"] == 0x23000000
