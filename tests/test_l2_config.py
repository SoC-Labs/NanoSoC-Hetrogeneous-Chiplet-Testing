"""L2 — one board, config-plane writes: CAM programming, role lock, reset of
config state.

Everything here is an APB write inside the TideLink config bank. **No data
crosses the link**, so there is no wedge risk: these are the same writes the
proven bring-up flow issues, and the config plane is reachable with the link up
or down.

Every test restores what it touched (`cam_sandbox`), so the module is
order-independent and leaves the bench exactly as it found it — which matters
because the translator resets on `hresetn`, not on POR, so a stale armed rule
would outlive the test that set it and silently change where a later cross-die
write lands.

Copyright (C) 2026, SoC Labs (www.soclabs.org)
"""
from __future__ import annotations

import pytest

from hetsoc import regs
from hetsoc.safety import AddressGuardError

import _helpers as H

pytestmark = [pytest.mark.l2, pytest.mark.hardware, pytest.mark.single_board]


@pytest.fixture
def cam_sandbox(each_board):
    """Snapshot the whole CAM channel-0 config and put it back afterwards."""
    saved = H.snapshot_cam(each_board)
    yield each_board
    H.restore_cam(each_board, saved)


def test_l2_cfg_01_cam_rule_write_readback(cam_sandbox):
    """L2-CFG-01: RULE_0 accepts and returns the exact translation word.

    Proves the one register the entire cross-die data plane depends on is
    writable and readable. If RULE_0 does not hold its value, every L4 transfer
    is really testing an identity map — and an untranslated peer address is not
    an inbound target on the far die, so it DECERRs.
    Pass: the words for both inbound regions read back bit-identical.
    """
    board = cam_sandbox
    target = board.target
    board.reg_write(regs.CAM_CTRL, 0)            # never arm a scratch rule
    for which in sorted(target.inbound_targets):
        want = regs.cam_rule(target.peer_aperture, target.inbound_byte(which))
        board.reg_write(regs.CAM_RULE_0, want)
        got = board.reg_read(regs.CAM_RULE_0)
        assert got == want, (
            "%s: RULE_0 <- 0x%08X (%s: 0x%02X -> 0x%02X) reads back 0x%08X"
            % (board.name, want, which, target.peer_aperture,
               target.inbound_byte(which), got))


def test_l2_cfg_02_cam_ctrl_and_base_offset_write_readback(cam_sandbox):
    """L2-CFG-02: CTRL.global_enable and BASE_OFFSET are RW.

    The two registers that arm the translator. `global_enable` is the master
    switch L4's CAM-off control test toggles; BASE_OFFSET is subtracted before
    matching, so a stuck non-zero value would shift every match byte and silently
    disable the rule while leaving RULE_0 looking correct.
    Pass: CTRL round-trips 1 and 0 in bit[0]; BASE_OFFSET round-trips an
    arbitrary 32-bit value and returns to 0.
    """
    board = cam_sandbox
    for want in (1, 0, 1, 0):
        board.reg_write(regs.CAM_CTRL, want)
        got = board.reg_read(regs.CAM_CTRL) & 1
        assert got == want, ("%s: CAM CTRL.global_enable <- %d reads %d"
                             % (board.name, want, got))
    for want in (0x2F000000, 0x00000004, 0x00000000):
        board.reg_write(regs.CAM_BASE, want)
        got = board.reg_read(regs.CAM_BASE)
        assert got == want, ("%s: CAM BASE_OFFSET <- 0x%08X reads 0x%08X"
                             % (board.name, want, got))


def test_l2_cfg_03_all_eight_rules_are_independent(cam_sandbox):
    """L2-CFG-03: RULE_0..RULE_7 are eight distinct registers.

    Proves the register-file geometry on silicon. An aliased or partially decoded
    rule file would make rule 0 — the only one a 16 MB aperture can ever match —
    depend on what some other rule happens to hold. That is invisible while only
    one rule is ever programmed, which is exactly how the flow uses it.
    Pass: eight distinct words written to eight rules read back individually
    unchanged.
    """
    board = cam_sandbox
    board.reg_write(regs.CAM_CTRL, 0)
    written = [regs.cam_rule(0x50 + n, 0x60 + n)
               for n in range(regs.CAM_NUM_RULES)]
    for index, value in enumerate(written):
        board.reg_write(regs.cam_rule_offset(index), value)
    got = [board.reg_read(regs.cam_rule_offset(i))
           for i in range(regs.CAM_NUM_RULES)]
    assert got == written, (
        "%s: CAM rules read back %s, wrote %s — the rule file aliases"
        % (board.name, ["0x%08X" % v for v in got],
           ["0x%08X" % v for v in written]))


def test_l2_cfg_04_rule_reserved_bits_read_zero(cam_sandbox):
    """L2-CFG-04: RULE_n retains only [0], [15:8] and [23:16].

    Proves the field masks in `tl_addr_trans_regs.sv` are real. If reserved bits
    were storage, a rule word built with a stray high bit would read back
    "correct" while meaning something else to the CAM — and [31:24] sits directly
    against the replace field the CAM writes into addr[31:24].
    Pass: writing all-ones leaves exactly the valid mask set.
    """
    board = cam_sandbox
    board.reg_write(regs.CAM_CTRL, 0)
    board.reg_write(regs.cam_rule_offset(1), 0xFFFFFFFF)
    got = board.reg_read(regs.cam_rule_offset(1))
    assert got == H.CAM_RULE_VALID_MASK, (
        "%s: RULE_1 <- 0xFFFFFFFF reads back 0x%08X, expected 0x%08X (enable[0] "
        "+ match[15:8] + replace[23:16] only)"
        % (board.name, got, H.CAM_RULE_VALID_MASK))


def test_l2_cfg_05_cam_returns_to_identity_state(cam_sandbox):
    """L2-CFG-05: the config state can be driven back to its reset/identity form.

    The translator resets on `hresetn`, not on POR, so nothing clears it between
    tests: "put it back" has to be an explicit, verified operation rather than an
    assumption. Without it, a rule armed by one test silently retargets a later
    test's cross-die write.
    Pass: after `clear_cam()`, CTRL, BASE_OFFSET and all eight rules read 0, and
    the CAM model agrees that state is an identity map.
    """
    board = cam_sandbox
    target = board.target
    H.clear_cam(board)
    board.reg_write(regs.CAM_RULE_0,
                    regs.cam_rule(target.peer_aperture,
                                  target.inbound_byte("shared_sram")))
    board.reg_write(regs.CAM_CTRL, 1)
    assert board.reg_read(regs.CAM_CTRL) & 1 == 1

    H.clear_cam(board)

    assert board.reg_read(regs.CAM_CTRL) & 1 == 0, "CTRL not cleared"
    assert board.reg_read(regs.CAM_BASE) == 0, "BASE_OFFSET not cleared"
    for index in range(regs.CAM_NUM_RULES):
        value = board.reg_read(regs.cam_rule_offset(index))
        assert value == 0, ("%s: RULE_%d still 0x%08X after clear"
                            % (board.name, index, value))

    peer = target.peer(H.XFER_OFFSET)
    assert H.translate(0, peer, global_enable=False) == peer


def test_l2_cfg_06_role_lock_cannot_be_flipped_at_runtime(each_board):
    """L2-CFG-06: once role_lock is set, a ROLE_CFG write cannot change the role.

    Proves the safety property the whole bring-up procedure rests on:
    `role_lock_reg` is W1S with a **POR-only** clear, and post-lock ROLE_CFG
    writes are gated on `!role_locked`. If a stray write could flip a live die
    from master to slave, the link would drop and the peer's in-flight
    transactions would never complete — i.e. it would wedge the *other* board.
    Pass: writing the OPPOSITE role leaves ROLE_STATUS bit-identical. Skips while
    role_lock is clear, because there the write would genuinely take effect.
    """
    board = each_board
    before = board.reg_read(regs.ROLE_STATUS)
    if not (before & H.ROLE_STATUS_LOCKED):
        pytest.skip(
            "%s: role_lock is clear (ROLE_STATUS=0x%08X). This test deliberately "
            "writes the WRONG role, which is only safe once the lock makes it a "
            "no-op. Bring the link up first (L3, or --deploy)."
            % (board.name, before))

    effective = before & H.ROLE_STATUS_EFFECTIVE
    opposite = (regs.ROLE_CFG_SLAVE_LOCK if effective == 0
                else regs.ROLE_CFG_MASTER_LOCK)
    board.reg_write(regs.ROLE_CFG, opposite)
    after = board.reg_read(regs.ROLE_STATUS)

    assert after == before, (
        "%s: ROLE_CFG <- 0x%02X changed ROLE_STATUS from 0x%08X to 0x%08X. "
        "role_lock must make post-lock role writes a no-op (POR-only clear); a "
        "die that can be re-roled at runtime can drop a live link out from under "
        "its peer." % (board.name, opposite, before, after))


def test_l2_cfg_07_tidechart_timeout_write_readback(each_board):
    """L2-CFG-07: TC_TIMEOUT is RW, so the election timeout can be widened.

    Proves the one poke the TideChart plan (G-TMO) requires before any election
    can succeed: the reset election timeout is 256 cycles, shorter than a D2D
    round-trip, so an election started at the default *always* times out.
    L3-TCHART-03 depends on this register accepting a wider value.
    Pass: TC_TIMEOUT round-trips the wide value, and is restored afterwards.
    """
    board = each_board
    address = board.target.tidechart(regs.TC_TIMEOUT)
    before = board.read(address)
    try:
        board.write(address, H.TC_TIMEOUT_WIDE)
        got = board.read(address)
        assert got == H.TC_TIMEOUT_WIDE, (
            "%s: TC_TIMEOUT <- 0x%08X reads back 0x%08X. Without a widened "
            "timeout every TideChart election times out at 256 cycles."
            % (board.name, H.TC_TIMEOUT_WIDE, got))
    finally:
        board.write(address, before)


@pytest.mark.pair
def test_l2_cfg_08_each_die_has_its_own_translator(pair):
    """L2-CFG-08: programming one die's CAM does not disturb the other's.

    Proves the translator is per-die and outbound-only — the inbound `ahb_mng`
    path is untranslated (`chp1` is tied off), so the rewrite happens exactly
    once, on the sender. Bidirectional traffic is only possible if the two CAMs
    are independent, and on a heterogeneous pair the two directions need
    genuinely *different* rules.
    No data crosses the link: these are config-plane APB writes on each die.
    Pass: after programming die A, die B's RULE_0 and CTRL are unchanged; then
    programming die B leaves die A's rule intact; and the two derived rules are
    the ones each direction actually needs.
    """
    a, b = pair.a, pair.b
    saved = {board.name: H.snapshot_cam(board) for board in pair.boards}
    try:
        H.clear_cam(a)
        H.clear_cam(b)
        b_ctrl, b_rule = (b.reg_read(regs.CAM_CTRL),
                          b.reg_read(regs.CAM_RULE_0))

        rule_a2b = pair.map_peer_to(a, "shared_sram")

        assert b.reg_read(regs.CAM_CTRL) == b_ctrl, (
            "programming %s's CAM changed %s's CTRL" % (a.name, b.name))
        assert b.reg_read(regs.CAM_RULE_0) == b_rule, (
            "programming %s's CAM changed %s's RULE_0" % (a.name, b.name))

        rule_b2a = pair.map_peer_to(b, "shared_sram")

        assert a.reg_read(regs.CAM_RULE_0) == rule_a2b, (
            "%s's RULE_0 changed when %s was programmed" % (a.name, b.name))
        assert b.reg_read(regs.CAM_RULE_0) == rule_b2a
        assert rule_a2b == H.cam_rule_between(a.target, b.target, "shared_sram")
        assert rule_b2a == H.cam_rule_between(b.target, a.target, "shared_sram")
    finally:
        for board in pair.boards:
            H.restore_cam(board, saved[board.name])


@pytest.mark.pair
def test_l2_cfg_09_cam_refuses_a_rule_the_far_die_cannot_decode(pair):
    """L2-CFG-09: `program_cam()` rejects a replace byte the FAR die does not
    decode, unless the caller says it means it.

    Proves the guard that makes the heterogeneous pair survivable. Inbound on the
    far die reaches exactly two regions; anything else takes a DECERR, and an
    unreturned response is how this path wedges the PS bus. So the framework must
    refuse a rule built from the *sender's* map — which is precisely the mistake
    a copied eth->eth mailbox rule makes against a compute die.
    Pass: an unmapped replace byte raises AddressGuardError; a wrong match byte
    (one that is not this die's peer aperture, so the rule could never fire)
    raises too; and `allow_unmapped=True` is honoured for the deliberate
    confinement test.
    """
    a, b = pair.a, pair.b
    saved = H.snapshot_cam(a)
    try:
        unmapped = next(byte for byte in (0x2C, 0x28, 0x21, 0x24)
                        if byte not in b.target.inbound_targets.values())
        with pytest.raises(AddressGuardError):
            pair.program_cam(a, a.target.peer_aperture, unmapped)

        with pytest.raises(AddressGuardError):
            pair.program_cam(a, (a.target.peer_aperture ^ 0x01) & 0xFF,
                             b.target.inbound_byte("shared_sram"))

        # The deliberate escape hatch, used by L4-DATA-09 with a POR staged.
        pair.program_cam(a, a.target.peer_aperture, unmapped,
                         enable=False, allow_unmapped=True)
        assert regs.decode_cam_rule(a.reg_read(regs.CAM_RULE_0))["replace"] \
            == unmapped
    finally:
        H.restore_cam(a, saved)
