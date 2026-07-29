# =============================================================================
# L0 — the target registry: address maths, the window guard, and the property
#      that makes the bare-link trap unreachable.
#
# THE FAILURE BEING TESTED FOR is not a wrong number in a report. On the KR260 a
# PS read of a PL address the SoC does not decode is an AXI transaction with no
# responder and NO TIMEOUT: the bus hangs, the board goes to 100% packet loss,
# and only a JTAG POR recovers it. The bare-link addresses (0x8000_0000,
# 0x8403_xxxx, 0xA400_xxxx) are exactly such addresses on a chiplet bitstream,
# and they wedged kr260_01 on first load. So the registry is tested for the
# STRUCTURAL property — no chiplet target can emit one for ANY input — not just
# for the happy path.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
import pytest

from hetsoc import regs
from hetsoc.safety import AddressGuardError, ConfigError, ProvisionalTargetError
from hetsoc.targets import (BARE_LINK_CEILING, BARE_LINK_TRAP_RANGES,
                            NO_PEER_APERTURE, TARGETS, Target, all_targets,
                            get_target, target_from_dict, target_names)

ETH = "kr260-eth-chiplet"
COMPUTE = "kr260-compute-chiplet"
BARE = "kr260-bare-link"


# =============================================================================
# Registry basics
# =============================================================================
class TestRegistry:
    def test_the_three_first_party_targets_are_present(self):
        assert set(target_names()) >= {ETH, COMPUTE, BARE}

    def test_unknown_target_raises_keyerror_and_never_falls_back(self):
        # tl_socmap.py's rule: never silently default. A wrong base is a hang.
        with pytest.raises(KeyError) as info:
            get_target("kr260-does-not-exist")
        assert "Refusing to guess" in str(info.value)

    def test_aliases_resolve(self):
        assert get_target("eth").name == ETH
        assert get_target("compute").name == COMPUTE
        assert get_target("bare").name == BARE

    def test_get_target_is_idempotent_on_a_target_object(self):
        target = get_target(ETH)
        assert get_target(target) is target


# =============================================================================
# eth chiplet — the affine backdoor window
# =============================================================================
class TestEthChipletAddressMaths:
    @pytest.fixture
    def target(self):
        return get_target(ETH)

    def test_window_is_the_hpm0_fpd_high_aperture(self, target):
        # tidelink.hwh:4112 — 0x4_0000_0000..0x4_FFFF_FFFF. NOT 0x8000_0000;
        # older comments say that and it is out-of-window (it would wedge).
        assert target.window_base == 0x4_0000_0000
        assert target.window_size == 0x1_0000_0000
        assert target.window_end == 0x5_0000_0000

    def test_to_host_is_base_plus_soc_address(self, target):
        assert target.to_host(0) == 0x4_0000_0000
        assert target.to_host(0x2E032108) == 0x4_2E03_2108   # SWI_LANE_STATUS
        assert target.to_host(0x2F001000) == 0x4_2F00_1000   # peer aperture
        assert target.to_host(0x2D001000) == 0x4_2D00_1000   # shared_sram_0

    def test_the_proven_silicon_addresses(self, target):
        # The exact addresses the on-silicon scripts poked, 2026-07-27.
        assert target.to_host(target.reg(regs.SWI_LANE_STATUS)) == 0x42E032108
        assert target.to_host(target.reg(regs.CAM_RULE_0)) == 0x42E034010

    def test_last_in_window_address_is_accepted(self, target):
        assert target.to_host(target.window_size - 4) == 0x4_FFFF_FFFC

    def test_one_past_the_window_is_refused(self, target):
        with pytest.raises(AddressGuardError) as info:
            target.to_host(target.window_size)
        assert "OUTSIDE" in str(info.value)
        assert "NO TIMEOUT" in str(info.value).upper()

    @pytest.mark.parametrize("bad", [-1, -0x1000])
    def test_negative_addresses_are_refused(self, target, bad):
        with pytest.raises(AddressGuardError):
            target.to_host(bad)

    @pytest.mark.parametrize("bad", [None, "0x2E032108", 1.5, True])
    def test_non_int_addresses_are_refused(self, target, bad):
        with pytest.raises(AddressGuardError):
            target.to_host(bad)

    def test_a_ps_physical_address_passed_by_mistake_is_refused(self, target):
        # Passing 0x4_2E03_2108 (already translated) would double-translate.
        with pytest.raises(AddressGuardError) as info:
            target.to_host(0x4_2E03_2108)
        assert "SoC-internal" in str(info.value)


# =============================================================================
# THE PROPERTY: a chiplet target can never emit a bare-link address
# =============================================================================
class TestNeverEmitsBareLinkAddresses:
    """The bare-link map is 32-bit; a chiplet backdoor is a HIGH aperture.

    So the guard is arithmetic, not a check that can be forgotten.
    """

    @staticmethod
    def _in_a_trap(address):
        return any(base <= address < base + size
                   for _name, base, size in BARE_LINK_TRAP_RANGES)

    def test_the_trap_ranges_cover_the_addresses_that_wedged_kr260_01(self):
        for wedger in (0x8000_0000, 0x8403_0000, 0x8403_2108, 0x8404_0000,
                       0xA400_0000, 0xA401_0000):
            assert self._in_a_trap(wedger), "0x%X must be a known trap" % wedger

    def test_no_chiplet_target_emits_a_trap_address_for_any_input(self):
        for target in all_targets():
            if target.kind != "chiplet" or not target.resolved:
                continue
            # Sweep: page 0, every 16 MB boundary, the aperture bytes, the edges,
            # and every address that WOULD produce a trap under a naive identity
            # or a sub-4GiB base.
            probes = [0, 4, 0x1000, target.window_size - 4]
            probes += [byte << 24 for byte in range(0x100)]
            probes += [0x8000_0000, 0x8403_0000, 0x8403_2108, 0xA400_0000,
                       0x4403_0000, 0x2E03_2108, 0x2F00_1000]
            probes += [i * 0x0100_0000 + 0x2108 for i in range(0x100)]
            for soc_addr in probes:
                try:
                    host = target.to_host(soc_addr)
                except AddressGuardError:
                    continue                    # refused outright: also fine
                assert not self._in_a_trap(host), (
                    "%s.to_host(0x%X) -> 0x%X lands in a bare-link aperture"
                    % (target.name, soc_addr, host))
                assert host >= BARE_LINK_CEILING, (
                    "%s.to_host(0x%X) -> 0x%X is a 32-bit address"
                    % (target.name, soc_addr, host))

    def test_a_chiplet_window_below_4gib_is_refused_at_construction(self):
        # This is the mechanism that makes the property structural: a descriptor
        # that COULD emit a bare-link address cannot be built at all.
        with pytest.raises(ConfigError) as info:
            Target(name="bad-chiplet", window_base=0x8000_0000,
                   window_size=0x1000_0000, kind="chiplet")
        assert "4 GiB" in str(info.value)
        assert "0x8403_xxxx" in str(info.value) or "0x8403" in str(info.value)

    def test_the_belt_and_braces_check_fires_if_the_invariant_is_bypassed(self):
        # Force a bad window past __post_init__ to prove to_host() still refuses.
        target = get_target(ETH)
        rogue = Target(name="rogue", window_base=target.window_base,
                       window_size=target.window_size, kind="chiplet")
        rogue.window_base = 0x8403_0000          # what a stale literal would do
        with pytest.raises(AddressGuardError) as info:
            rogue.to_host(0x0)
        assert "BARE-LINK" in str(info.value)

    def test_the_bare_link_target_itself_may_emit_them(self):
        # It is the non-chiplet control design; those addresses are its job.
        bare = get_target(BARE)
        assert bare.to_host(0x4403_0000) == 0x8403_0000
        assert bare.to_host(0x4000_0000) == 0x8000_0000
        assert bare.to_host(0x8400_0000) == 0xA400_0000


# =============================================================================
# bare-link — aperture relocation (tl_socmap semantics)
# =============================================================================
class TestBareLinkTarget:
    @pytest.fixture
    def target(self):
        return get_target(BARE)

    def test_relocates_within_an_aperture(self, target):
        assert target.to_host(0x4403_2108) == 0x8403_2108
        assert target.to_host(0x4403_7FFF) == 0x8403_7FFF

    def test_address_outside_every_aperture_is_refused(self, target):
        with pytest.raises(AddressGuardError) as info:
            target.to_host(0x5000_0000)
        assert "not inside any known aperture" in str(info.value)
        assert "AXI hang" in str(info.value)

    def test_apertures_do_not_overlap(self, target):
        # tl_socmap's note: ahb_sub is 64 MB so it does NOT swallow the 0x4403
        # APB aperture. An ambiguous table would relocate to the wrong place.
        spans = [(base, base + size) for _n, base, _h, size in target.apertures]
        for i, (lo_a, hi_a) in enumerate(spans):
            for lo_b, hi_b in spans[i + 1:]:
                assert hi_a <= lo_b or hi_b <= lo_a, "apertures overlap"

    def test_it_has_no_peer_window(self, target):
        assert target.peer_aperture == NO_PEER_APERTURE
        assert target.is_peer(0x0000_0000) is False
        assert target.is_peer(0x2F00_1000) is False
        with pytest.raises(AddressGuardError):
            target.peer(0)


# =============================================================================
# Peer aperture + inbound modelling
# =============================================================================
class TestPeerAndInbound:
    @pytest.fixture
    def target(self):
        return get_target(ETH)

    def test_peer_aperture_is_0x2f(self, target):
        assert target.peer_aperture == 0x2F

    @pytest.mark.parametrize("addr,expect", [
        (0x2F00_0000, True), (0x2F00_1000, True), (0x2FFF_FFFF, True),
        (0x2E03_2108, False),           # TideLink config — local, not peer
        (0x2D00_1000, False),           # this die's OWN shared_sram_0
        (0x2300_0000, False),           # this die's OWN mailbox
        (0x0, False), (-1, False),
    ])
    def test_is_peer(self, target, addr, expect):
        assert target.is_peer(addr) is expect

    def test_peer_builds_an_address_in_the_window(self, target):
        assert target.peer(0x1000) == 0x2F001000     # the proven PEER_ADDR
        assert target.peer(0) == 0x2F000000

    def test_peer_offset_beyond_16mb_is_refused(self, target):
        # The CAM's granularity is one addr[31:24] value: 16 MB, no more.
        with pytest.raises(AddressGuardError):
            target.peer(1 << 24)

    def test_exactly_two_inbound_targets(self, target):
        # nanosoc_multicore_soc.yaml:2383-2387 — everything else DECERRs.
        assert target.inbound_targets == {"shared_sram": 0x2D, "ipc_mailbox": 0x23}
        assert len(target.inbound_targets) == 2

    def test_inbound_lookup_accepts_the_rtl_instance_names(self, target):
        assert target.inbound_byte("shared_sram_0") == 0x2D
        assert target.inbound_byte("ipc_mailbox_0") == 0x23
        assert target.inbound_soc_base("shared_sram") == 0x2D000000
        assert target.inbound_soc_base("ipc_mailbox") == 0x23000000

    @pytest.mark.parametrize("third", ["dmac_0", "qspi_flash_0", "0x2C",
                                       "shared_sram_1", ""])
    def test_a_third_inbound_target_is_refused(self, target, third):
        with pytest.raises(AddressGuardError) as info:
            target.inbound_byte(third)
        assert "EXACTLY" in str(info.value)

    def test_a_descriptor_cannot_declare_a_third_inbound_target(self):
        with pytest.raises(ConfigError) as info:
            Target(name="three-inbound", window_base=0x4_0000_0000,
                   window_size=0x1000,
                   inbound_targets={"shared_sram": 0x2D, "ipc_mailbox": 0x23,
                                    "dmac": 0x20})
        assert "EXACTLY two" in str(info.value)

    def test_cam_rule_for_builds_the_proven_words(self, target):
        assert target.cam_rule_for("shared_sram") == 0x002D2F01
        assert target.cam_rule_for("ipc_mailbox") == 0x00232F01


# =============================================================================
# THE HETEROGENEITY that this repo exists for
# =============================================================================
class TestHeterogeneousPairAddressing:
    def test_the_two_dies_disagree_about_the_mailbox_byte(self):
        eth = get_target(ETH)
        compute = get_target(COMPUTE)
        assert eth.inbound_byte("ipc_mailbox") == 0x23
        assert compute.inbound_byte("ipc_mailbox") == 0x2A
        assert eth.inbound_byte("shared_sram") == compute.inbound_byte("shared_sram")

    def test_a_cross_die_rule_takes_the_replace_byte_from_the_receiver(self):
        eth = get_target(ETH)
        compute = get_target(COMPUTE)
        # eth -> eth (homogeneous, proven on silicon)
        assert eth.cam_rule_for("ipc_mailbox", far_target=eth) == 0x00232F01
        # eth -> compute (heterogeneous): 0x2A, not 0x23. Getting this wrong
        # aims the write at a region the compute die does not decode.
        assert eth.cam_rule_for("ipc_mailbox", far_target=compute) == 0x002A2F01

    def test_the_compute_die_originates_receiver_keyed_cross_die_rules(self):
        # Post-G4 the compute die has a peer aperture (0x41), so it can now
        # ORIGINATE cross-die rules. Per G7 the replace byte is the RECEIVER's
        # (eth's) map: mailbox 0x23, shared_sram 0x2D — so compute->eth is
        # cam_rule(0x41, ...).
        compute = get_target(COMPUTE)
        eth = get_target(ETH)
        assert compute.cam_rule_for("ipc_mailbox", far_target=eth) == 0x00234101
        assert compute.cam_rule_for("shared_sram", far_target=eth) == 0x002D4101
        # The eth-habit rule (replace 0x23) is INVALID at a COMPUTE receiver:
        # 0x23 is not in compute's inbound set, so no cam_rule_for(...) aimed at
        # compute can ever produce a 0x??->0x23 rule (it would DECERR).
        assert 0x23 not in compute.inbound_targets.values()
        for which in ("shared_sram", "ipc_mailbox"):
            assert compute.inbound_byte(which) != 0x23


# =============================================================================
# The provisional compute chiplet
# =============================================================================
class TestProvisionalComputeTarget:
    @pytest.fixture
    def target(self):
        return get_target(COMPUTE)

    def test_it_is_marked_provisional_with_a_reason(self, target):
        assert target.provisional is True
        assert target.resolved is False
        assert "no FPGA/KR260 port" in target.provisional_reason.lower() \
            or "NO FPGA/KR260 port" in target.provisional_reason

    def test_every_address_translation_fails_loud(self, target):
        for soc_addr in (0x0, 0x2E032108, 0x2D000000, 0x40030000):
            with pytest.raises(ProvisionalTargetError) as info:
                target.to_host(soc_addr)
            assert "NO verified PS address window" in str(info.value)
            assert "hetsoc.toml" in str(info.value)

    def test_provisional_error_is_an_address_guard_error(self, target):
        # so callers guarding on the base class still stop.
        with pytest.raises(AddressGuardError):
            target.to_host(0)

    def test_it_carries_the_soc_internal_map_it_could_verify(self, target):
        # Safe to carry: SoC-internal bytes never reach /dev/mem on their own,
        # and the heterogeneous pair needs the far-die inbound bytes.
        assert target.inbound_targets == {"shared_sram": 0x2D, "ipc_mailbox": 0x2A}

    def test_it_declares_the_post_g4_link0_peer_aperture(self, target):
        # Post-G4 (docs/BRINGUP_GAPS.md §G4): compute's D2D window is
        # 0x4000_0000, so haddr[24]==1 -> peer 0x41 (link 0) / 0x61 (link 1).
        # Encoded as the post-decoder INTENDED value; it is SoC-internal only,
        # and to_host() stays fail-loud until G2 (see the provisional tests
        # above), so an unconfirmed value cannot wedge a board.
        assert target.peer_aperture == 0x41
        assert target.peer_aperture % 2 == 1        # the odd (peer) half
        link_peers = {row[0]: row[5] for row in target.d2d_links}
        assert link_peers == {"link0": 0x41, "link1": 0x61}

    def test_it_declares_no_bootrom_probe(self, target):
        assert target.bootrom_soc_base is None
        assert target.bootrom_expect == ()

    def test_a_provisional_target_must_say_what_is_unknown(self):
        with pytest.raises(ConfigError):
            Target(name="mystery", window_base=0, window_size=0, provisional=True)


# =============================================================================
# TOML overrides
# =============================================================================
class TestTargetOverrides:
    def test_hex_strings_are_accepted_for_addresses(self):
        target = target_from_dict("kr260-eth-chiplet-x",
                                  {"inherit": ETH, "window_base": "0x500000000"})
        assert target.window_base == 0x5_0000_0000

    def test_an_override_inherits_the_rest_of_the_descriptor(self):
        target = target_from_dict("eth-copy", {"inherit": ETH})
        assert target.peer_aperture == 0x2F
        assert target.inbound_targets == {"shared_sram": 0x2D, "ipc_mailbox": 0x23}

    def test_unknown_keys_are_refused(self):
        with pytest.raises(ConfigError) as info:
            target_from_dict("eth-typo", {"inherit": ETH, "windowbase": 1})
        assert "unknown key" in str(info.value)

    def test_resolving_the_compute_target_requires_a_citation(self):
        # A window base that can wedge a board must say where it came from.
        with pytest.raises(ConfigError) as info:
            target_from_dict(COMPUTE, {"inherit": COMPUTE,
                                       "window_base": "0x400000000",
                                       "window_size": "0x100000000"})
        assert "cite where it came from" in str(info.value)

    def test_resolving_with_a_citation_works_and_clears_provisional(self):
        target = target_from_dict(COMPUTE, {
            "inherit": COMPUTE,
            "window_base": "0x400000000",
            "window_size": "0x100000000",
            "source": "compute tidelink.hwh:1234 MEMRANGE",
        })
        assert target.provisional is False
        assert target.resolved is True
        assert target.to_host(0x2D000000) == 0x4_2D00_0000

    def test_an_override_cannot_smuggle_in_a_sub_4gib_chiplet_window(self):
        with pytest.raises(ConfigError):
            target_from_dict(COMPUTE, {"inherit": COMPUTE,
                                       "window_base": "0x80000000",
                                       "window_size": "0x10000000",
                                       "source": "made up"})

    def test_registry_is_not_mutated_by_building_an_override(self):
        target_from_dict("scratch", {"inherit": ETH})
        assert "scratch" not in TARGETS


# =============================================================================
# Descriptor plumbing
# =============================================================================
class TestDescriptorHelpers:
    def test_reg_composes_the_per_target_tlapb_base(self):
        assert get_target(ETH).reg(regs.SWI_LANE_STATUS) == 0x2E032108
        # The compute die's TideLink APB is NOT at 0x2E03_0000 — 0x2E is
        # mgr_remap_0, a live peripheral, on that die.
        assert get_target(COMPUTE).reg(regs.SWI_LANE_STATUS) != 0x2E032108

    def test_describe_mentions_the_provisional_state(self):
        text = get_target(COMPUTE).describe()
        assert "PROVISIONAL" in text

    def test_describe_lists_the_window_for_a_resolved_target(self):
        assert "0x400000000" in get_target(ETH).describe().replace("_", "")
