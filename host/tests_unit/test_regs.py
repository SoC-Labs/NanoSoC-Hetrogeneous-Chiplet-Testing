# =============================================================================
# L0 — register offsets, the CAM rule encoder, and the lane-status decoder.
#
# These are checked against values that came off real silicon or out of the RTL,
# not against the implementation. A CAM rule with the match and replace bytes
# transposed aims cross-die traffic at a region the far die does not decode; the
# far die DECERRs, the response never returns, and the PS bus wedges. So the
# encoder gets tested against the literal words the proven scripts write.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
import pytest

from hetsoc import regs


# =============================================================================
# CAM rule encoder
# =============================================================================
class TestCamRule:
    def test_shared_sram_rule_matches_the_proven_word(self):
        # kr260_eth_xfer.py:55 RULE_0_VALUE, and PEER_APERTURE_PROGRAMMING.md §4:
        # enable=1, match=0x2F (peer aperture), replace=0x2D (far shared_sram_0).
        assert regs.cam_rule(0x2F, 0x2D) == 0x002D2F01

    def test_mailbox_rule_matches_the_proven_word(self):
        # kr260_eth_xfer.py:69 MBOX_RULE_VALUE — 0x2F -> 0x23 ipc_mailbox_0.
        assert regs.cam_rule(0x2F, 0x23) == 0x00232F01

    def test_disable_clears_only_the_enable_bit(self):
        assert regs.cam_rule(0x2F, 0x2D, enable=False) == 0x002D2F00

    def test_field_positions(self):
        rule = regs.cam_rule(0xAB, 0xCD)
        assert (rule >> 8) & 0xFF == 0xAB, "match byte is [15:8]"
        assert (rule >> 16) & 0xFF == 0xCD, "replace byte is [23:16]"
        assert rule & 1 == 1, "enable is [0]"
        assert (rule >> 24) & 0xFF == 0, "[31:24] is reserved"
        assert (rule >> 1) & 0x7F == 0, "[7:1] is reserved"

    def test_match_and_replace_are_not_transposed(self):
        # The failure this guards: a transposed rule sends 0x2D-window traffic
        # to 0x2F on the far die, which the far die does not decode.
        assert regs.cam_rule(0x2F, 0x2D) != regs.cam_rule(0x2D, 0x2F)

    def test_round_trips_through_the_decoder(self):
        for match, replace, enable in ((0x2F, 0x2D, True), (0x2F, 0x23, True),
                                       (0x41, 0x2A, False), (0x00, 0xFF, True)):
            decoded = regs.decode_cam_rule(regs.cam_rule(match, replace, enable))
            assert decoded["match"] == match
            assert decoded["replace"] == replace
            assert decoded["enable"] == int(enable)

    @pytest.mark.parametrize("bad", [0x100, 0x1FF, -1, 0x2F00])
    def test_rejects_non_byte_values(self, bad):
        # Silent truncation here would arm a rule pointing somewhere else.
        with pytest.raises(ValueError):
            regs.cam_rule(bad, 0x2D)
        with pytest.raises(ValueError):
            regs.cam_rule(0x2F, bad)

    @pytest.mark.parametrize("bad", [None, "0x2F", 1.5, True])
    def test_rejects_non_int_values(self, bad):
        with pytest.raises(ValueError):
            regs.cam_rule(bad, 0x2D)

    def test_rule_offsets_are_word_spaced_from_rule_0(self):
        assert regs.cam_rule_offset(0) == regs.CAM_RULE_0
        assert regs.cam_rule_offset(7) == regs.CAM_RULE_0 + 7 * 4
        with pytest.raises(ValueError):
            regs.cam_rule_offset(8)          # NUM_RULES == 8, channel 0 only


# =============================================================================
# Lane-status decoder
# =============================================================================
class TestLaneStatusDecoder:
    def test_silicon_die_a_value_decodes_to_link_up(self):
        # SWI_LANE_STATUS die_a = 0x05890000, measured on silicon 2026-07-27.
        status = regs.decode_lane_status(0x05890000)
        assert status.fcsm == regs.FCSM_LINK_IDLE == 4
        assert status.cal_done == 1
        assert status.cr_seen == 1 and status.crack_seen == 1
        assert status.link_up is True

    def test_silicon_die_b_value_decodes_to_link_up(self):
        status = regs.decode_lane_status(0x27890000)
        assert status.fcsm == 4 and status.cal_done == 1
        assert status.link_up is True

    def test_lane_locked_reads_zero_after_training_and_is_not_the_gate(self):
        # lane_locked self-deasserts to 0x00 after training. Judging link health
        # by lane-lock instead of FCSM reports a healthy link as down.
        status = regs.decode_lane_status(0x05890000)
        assert status.lane_locked == 0x00
        assert status.link_up is True

    def test_field_extraction(self):
        status = regs.decode_lane_status(0x018B_A5C3)
        assert status.lane_locked == 0xC3          # [7:0]
        assert status.lane_fault == 0xA5           # [15:8]
        assert status.cal_done == 1                # [16]
        assert status.fcsm == (0x018B >> 1) & 0x7  # [19:17]

    @pytest.mark.parametrize("fcsm,cal,expect", [
        (4, 1, True),      # the only up state
        (4, 0, False),     # FCSM says idle but calibration never completed
        (3, 1, False),
        (0, 0, False),
    ])
    def test_link_up_requires_fcsm_4_and_cal_done(self, fcsm, cal, expect):
        raw = (fcsm << 17) | (cal << 16)
        assert regs.decode_lane_status(raw).link_up is expect

    def test_fcsm_4_is_named_link_idle(self):
        assert regs.decode_lane_status(4 << 17).fcsm_name == "LINK_IDLE"

    def test_as_dict_is_json_safe(self):
        payload = regs.decode_lane_status(0x05890000).as_dict()
        assert payload["fcsm"] == 4 and payload["link_up"] == 1
        assert all(isinstance(v, int) for v in payload.values())

    def test_equality_and_hash_follow_the_raw_word(self):
        assert regs.decode_lane_status(0x1234) == regs.decode_lane_status(0x1234)
        assert regs.decode_lane_status(0x1234) != regs.decode_lane_status(0x5678)
        assert len({regs.decode_lane_status(1), regs.decode_lane_status(1)}) == 1


# =============================================================================
# Offsets — cross-checked against the absolute addresses the proven scripts use
# =============================================================================
class TestOffsets:
    @pytest.mark.parametrize("offset,absolute,source", [
        (regs.SWI_LANE_STATUS, 0x2E032108, "kr260_eth_bringup.py:83"),
        (regs.ROLE_CFG, 0x2E032080, "PEER_APERTURE_PROGRAMMING.md §6 (NOT 0x2084)"),
        (regs.ROLE_STATUS, 0x2E032084, "STATUS_REGISTERS.md §1"),
        (regs.CREDIT_COUNT, 0x2E03200C, "kr260_eth_xfer.py:72"),
        (regs.STATUS, 0x2E032010, "kr260_eth_xfer.py:73"),
        (regs.OBS_FC_CREDIT, 0x2E03219C, "kr260_eth_xfer.py:74"),
        (regs.CAM_BASE, 0x2E034000, "PEER_APERTURE_PROGRAMMING.md §3"),
        (regs.CAM_CTRL, 0x2E034004, "PEER_APERTURE_PROGRAMMING.md §3"),
        (regs.CAM_RULE_0, 0x2E034010, "PEER_APERTURE_PROGRAMMING.md §3"),
        (regs.WL_LINK_ENABLE_RESET, 0x2E030208, "kr260_eth_bringup.py:79"),
        (regs.SWI_TRAINING_MODE, 0x2E032100, "kr260_eth_bringup.py:82"),
        (regs.WLINK_LINK_STATUS, 0x2E030234, "STATUS_REGISTERS.md §1"),
    ])
    def test_offset_plus_base_matches_the_documented_absolute(self, offset,
                                                              absolute, source):
        assert regs.TLAPB_BASE + offset == absolute, source

    def test_role_cfg_is_0x2080_not_0x2084(self):
        # INTEGRATION_GUIDE.md says 0x2084; that lands on the next register and
        # the role silently never locks.
        assert regs.ROLE_CFG == 0x2080
        assert regs.ROLE_CFG != regs.ROLE_STATUS

    def test_ll_bootstrap_values(self):
        # 3-write sequence; swreset FIRST clears the CR/CRACK stickies.
        assert (regs.LL_SWRESET_ON, regs.LL_SWRESET_OFF, regs.LL_ENABLE) == \
               (0x00027F08, 0x00027F00, 0x00027F07)

    def test_role_lock_constants(self):
        assert regs.ROLE_CFG_MASTER_LOCK == 0x02   # role_lock=1, role=0
        assert regs.ROLE_CFG_SLAVE_LOCK == 0x03    # role_lock=1, role=1

    def test_fc_node_table_matches_the_proven_bases(self):
        assert dict(regs.FC_NODES) == {
            "AW": 0x1000, "W": 0x1100, "B": 0x1200, "AR": 0x1300,
            "R": 0x1400, "GenBus": 0x1600, "TideLink": 0x1700}

    def test_only_the_axi_data_nodes_are_the_recovery_stripped_set(self):
        # The AXI nodes ship the upstream FCSM with no recovery; GenBus and the
        # TideLink sideband keep it. A stuck FIFO means different things on each.
        assert set(regs.FC_AXI_DATA_NODES) == {"AW", "W", "B", "AR", "R"}
        assert "TideLink" not in regs.FC_AXI_DATA_NODES

    def test_sticky_mask_covers_bits_3_1(self):
        assert regs.STATUS_STICKY_MASK == 0xE
        decoded = regs.decode_sticky_status(0xE)
        assert decoded["overrun"] and decoded["underrun"] and decoded["master_error"]
        assert regs.decode_sticky_status(0x1)["sticky"] == 0   # busy is not sticky

    def test_fc_node_reg_offsets(self):
        assert regs.fc_node_regs(0x1200) == {
            "txfifo": 0x1208, "acknack": 0x1210, "crc": 0x1220}
