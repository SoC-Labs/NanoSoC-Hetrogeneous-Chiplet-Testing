# =============================================================================
# tests_unit/conftest.py — L0 fixtures.
#
# EVERY test under tests_unit/ must pass with NO hardware, NO network and NO
# third-party packages beyond pytest. That is not a convenience: the address
# maths, the registry guards and the CAM encoder are the code that stands
# between a test and a wedged board, so they have to be verifiable somewhere
# that isn't the bench.
#
# The fake board is a `MemoryTransport` preloaded with the REAL register values
# read off silicon on 2026-07-27 (SWI_LANE_STATUS die_a=0x05890000,
# die_b=0x27890000) so the decoders are checked against reality, not against a
# value invented to make them pass.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""Shared L0 fixtures: fake boards backed by an in-process memory transport."""

import copy

import pytest

from hetsoc import regs, targets as targets_mod
from hetsoc.board import Board
from hetsoc.pair import ChipletPair
from hetsoc.transport import MemoryTransport

#: Measured on real silicon, 2026-07-27 (KR260_BENCH_RUNBOOK.md §9):
#: FCSM=4 (LINK_IDLE), cal_done=1, cr_seen=crack_seen=1 on both dies.
LANE_STATUS_UP_A = 0x05890000
LANE_STATUS_UP_B = 0x27890000
LANE_STATUS_DOWN = 0x00000000

#: eth_ss_probe.py:15 — the boot-ROM vector table words this design reads back.
BOOTROM_WORDS = (0x18003C00, 0x08000189, 0x080001CD, 0x080001CF)


@pytest.fixture(autouse=True)
def _restore_registry():
    """Undo any registry mutation a test makes (config overrides do this)."""
    saved = copy.deepcopy(targets_mod.TARGETS)
    yield
    targets_mod.TARGETS.clear()
    targets_mod.TARGETS.update(saved)


def make_memory(target, link_up=True, lane_status=None, role_effective=0):
    """A MemoryTransport preloaded so a board looks alive and (optionally) up."""
    mem = MemoryTransport(target.window_base, target.window_size)

    def poke(soc_addr, value):
        mem.mem[target.to_host(soc_addr)] = value

    if lane_status is None:
        lane_status = LANE_STATUS_UP_A if link_up else LANE_STATUS_DOWN
    poke(target.reg(regs.SWI_LANE_STATUS), lane_status)
    poke(target.reg(regs.ROLE_STATUS), (role_effective & 1) | 0x2)
    poke(target.reg(regs.STATUS), 0x00000000)
    poke(target.reg(regs.CREDIT_COUNT), 4096)
    poke(target.reg(regs.OBS_FC_CREDIT), 0x00000000)
    poke(target.reg(regs.SYNC_DET), 0x00000000)
    poke(target.reg(regs.WLINK_LINK_STATUS), regs.WLINK_TX_LANES_ACTIVE
         | regs.WLINK_RX_DATA_VALID)
    for _name, base in regs.FC_NODES:
        poke(target.reg(base + regs.FC_TXFIFO), 1)      # TX FIFO empty
        poke(target.reg(base + regs.FC_ACKNACK), 1)     # Ack/Nack FIFO empty
        poke(target.reg(base + regs.FC_CRC), 0)         # no CRC errors
    if target.bootrom_soc_base is not None and target.bootrom_expect:
        for index, word in enumerate(target.bootrom_expect):
            poke(target.bootrom_soc_base + 4 * index, word)
    return mem


def make_board(name="eth", role="die_a", target="kr260-eth-chiplet",
               link_up=True, lane_status=None, host=None, fpgahub="kr260_01"):
    """A Board wired to a fake memory transport — no ssh, no /dev/mem."""
    resolved = targets_mod.get_target(target)
    role_effective = 0 if role == "die_a" else 1
    if lane_status is None and link_up:
        lane_status = LANE_STATUS_UP_A if role == "die_a" else LANE_STATUS_UP_B
    mem = make_memory(resolved, link_up=link_up, lane_status=lane_status,
                      role_effective=role_effective)
    board = Board(host=host or "ubuntu@10.22.24.159", target=resolved, role=role,
                  name=name, fpgahub=fpgahub, transport=mem, timeout_s=5.0)
    return board


@pytest.fixture
def eth_board():
    return make_board("eth", "die_a")


@pytest.fixture
def down_board():
    return make_board("eth", "die_a", link_up=False)


@pytest.fixture
def homogeneous_pair():
    """The pair that is proven on silicon: eth die_a <-> eth die_b."""
    a = make_board("eth_a", "die_a", host="ubuntu@10.22.24.159", fpgahub="kr260_01")
    b = make_board("eth_b", "die_b", host="ubuntu@10.22.24.153", fpgahub="kr260_02")
    return ChipletPair(a, b)
