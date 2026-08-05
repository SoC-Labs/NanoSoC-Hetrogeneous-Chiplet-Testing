# =============================================================================
# hetsoc.anchor — the deskew SYNC anchor (H1).
#
# WHAT THIS IS FOR
# ----------------
# After bring-up the pair reaches fcsm=4 but the deskew is NOT anchored:
# `EPOCH_STATUS` bit0 (`reanchored`) reads 0 on both dies and R8 is 0, so no
# SYNC beacon is being emitted. The winscan FSM has its own anchor gate
# (`WS_FINALIZE`, which holds `winscan_done` until `reanchored=1`) but it fires
# DURING the scan — before both dies are up — so it times out and releases
# (`ws_anchor_timeout_q`). Nothing emits SYNC afterwards, so `reanchored` stays
# 0 and the initiator can hang on a cross-die write.
#
# The host-side remedy, proven 2026-08-03: a `force_always` burst on R8 AFTER
# fcsm=4, which anchors the deskew (`reanchored` 0->1). With it, 300-beat and
# 200-beat soaks run clean; without it a plain write wedges on some deploys.
#
# Source: docs/PROPOSAL_AUTO_ANCHOR_RTL_2026_08_04.md (eth chiplet repo) —
# "host writes R8 0x2E03_2100=0x1C on both dies, then 0x00"; observable is
# EPOCH_STATUS 0x2140 bit0. That proposal exists to make this automatic in RTL;
# until it lands, every host-driven session must do it explicitly.
#
# THIS IS A WORKAROUND, AND IT IS NOT THE WHOLE WEDGE STORY. A residual
# intermittent wedge remains after anchoring — an eye-margin / deskew-alignment
# lottery rather than an AXI-recovery bug (docs/REPLY_AXIREC_RECONCILE_2026_08_04.md).
# Anchoring improves the odds substantially; it does not make the data plane
# unattended-safe.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""Deskew SYNC anchor — the post-bring-up step that makes cross-die writes land."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Dict

from . import regs
from .safety import LinkDownError

if TYPE_CHECKING:  # pragma: no cover
    from .board import Board

__all__ = ["R8_FORCE_ALWAYS", "EPOCH_STATUS", "EPOCH_REANCHORED",
           "anchor_die", "anchor_pair", "is_anchored"]

# R8 / SWI_TRAINING_MODE, TLAPB-relative. 0x1C is the force_always burst
# pattern the proven host sequence writes; 0x00 releases it.
R8_TRAINING_MODE = 0x2100
R8_FORCE_ALWAYS = 0x1C
R8_RELEASE = 0x00

# EPOCH_STATUS, TLAPB-relative. bit0 = reanchored.
EPOCH_STATUS = 0x2140
EPOCH_REANCHORED = 1 << 0

# The proven sequence holds the burst for ~0.4 s before releasing.
ANCHOR_HOLD_S = 0.4


def is_anchored(board: "Board") -> bool:
    """True if this die reports its deskew re-anchored."""
    return bool(board.reg_read(EPOCH_STATUS) & EPOCH_REANCHORED)


def anchor_die(board: "Board", hold_s: float = ANCHOR_HOLD_S,
               require: bool = True) -> bool:
    """Pulse the force_always burst on one die and report whether it anchored.

    Must be called AFTER the link reaches fcsm=4 — the burst is what the
    post-scan link never emits on its own, and before fcsm=4 there is nothing
    for the peer to anchor against.

    `require=False` returns the outcome instead of raising, for the diagnostic
    path that wants to record "anchoring did not take" rather than abort.
    """
    if not board.link_up():
        raise LinkDownError(
            "%s: refusing to anchor a link that is not up. The SYNC burst is a "
            "POST-bring-up step (fcsm must be 4); issuing it earlier anchors "
            "nothing and hides the real bring-up failure." % board.name)

    board.reg_write(R8_TRAINING_MODE, R8_FORCE_ALWAYS)
    time.sleep(hold_s)
    board.reg_write(R8_TRAINING_MODE, R8_RELEASE)

    ok = is_anchored(board)
    if require and not ok:
        raise LinkDownError(
            "%s: EPOCH_STATUS bit0 (reanchored) still 0 after the force_always "
            "burst. Cross-die writes from this die may wedge the bus.\n"
            "  wrote R8 (TLAPB+0x%04X) = 0x%02X, held %.2f s, released 0x00\n"
            "  EPOCH_STATUS (TLAPB+0x%04X) = 0x%08X\n"
            "  See docs/PROPOSAL_AUTO_ANCHOR_RTL_2026_08_04.md."
            % (board.name, R8_TRAINING_MODE, R8_FORCE_ALWAYS, hold_s,
               EPOCH_STATUS, board.reg_read(EPOCH_STATUS)))
    return ok


def anchor_pair(pair, hold_s: float = ANCHOR_HOLD_S,
                require: bool = True) -> Dict[str, bool]:
    """Anchor BOTH dies. Returns {board_name: anchored}.

    Both, deliberately: the anchor is a property of each die's own deskew, and
    an asymmetrically-anchored pair is the configuration the wedge reports come
    from. Anchoring only the sender is a plausible-looking mistake.
    """
    out = {}
    for board in (pair.a, pair.b):
        out[board.name] = anchor_die(board, hold_s=hold_s, require=require)
    return out


def anchor_report(pair) -> str:
    """One-line status for a log or a dashboard."""
    bits = []
    for board in (pair.a, pair.b):
        try:
            bits.append("%s=%s" % (board.name,
                                   "anchored" if is_anchored(board) else "NOT-anchored"))
        except Exception as exc:                      # noqa: BLE001 - diagnostic
            bits.append("%s=unreadable(%s)" % (board.name, type(exc).__name__))
    return "deskew anchor: " + " ".join(bits)


# Keep the shared register table honest: if regs ever gains these offsets,
# prefer them over the local copies so there is one source of truth.
for _name, _local in (("SWI_TRAINING_MODE", R8_TRAINING_MODE),
                      ("EPOCH_STATUS", EPOCH_STATUS)):
    _shared = getattr(regs, _name, None)
    if _shared is not None and _shared != _local:  # pragma: no cover
        raise ImportError(
            "hetsoc.anchor disagrees with hetsoc.regs on %s "
            "(anchor 0x%04X, regs 0x%04X). One of them is wrong; do not guess."
            % (_name, _local, _shared))
