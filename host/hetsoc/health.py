# =============================================================================
# hetsoc.health — the diagnostic that catches the known cross-die wedge.
#
# WHY THIS IS NOT JUST "READ SOME COUNTERS"
# -----------------------------------------
# docs/CROSS_DIE_WEDGE_ROOTCAUSE.md (2026-07-29) root-caused the intermittent
# two-board wedge: the silicon build ships the UPSTREAM, recovery-stripped FCSM
# on the five AXI data-plane flow-control nodes (AW/W/B/AR/R); only the TideLink
# sideband node keeps the SoC-Labs recovery logic (socl_reack, the state-7
# watchdog, the L9b/L9c pktnum re-anchor). So one bit error or one dropped ACK
# on an AXI node has NO recovery path: the node stops emitting, the far side's
# B (write) or R (read) beat never returns, the PS SmartConnect saturates, and
# the whole PL slave set wedges until a JTAG POR.
#
# THE TRAP THIS MODULE EXISTS TO AVOID
# ------------------------------------
#   OBS_FC_CREDIT (0x219C) and SWI_LANE_STATUS[31:17] observe the SIDEBAND node
#   ONLY. They do NOT see the AXI nodes that wedge.
# A run that polls only those reports a perfectly healthy link right up to the
# moment the board dies. `CREDIT_COUNT` reading 4096 (idle FIFO) during a soak
# is the proof: the peer window does not use that path at all, which is also why
# RELEASE_THRESHOLD tuning cannot affect the wedge.
#
# The per-node registers below are the only visibility that exists. Read them
# BETWEEN transfers, before a hang:
#   * rising CRC on B or R          -> bit error (calibration drift / marginal eye)
#   * stuck non-empty Ack/Nack FIFO -> credit/ACK stall (the recovery gap)
# All reads are RO and in-window, so this function is wedge-safe by construction
# and can run at L1 on a single board.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""Link and per-node flow-control health readout (read-only, wedge-safe)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import regs
from .log import get_logger

__all__ = ["fc_health", "link_health", "verdict", "format_health",
           "compare_fc_health"]

_log = get_logger("health")


# =============================================================================
def fc_health(board: Any) -> Dict[str, Any]:
    """Read the per-node Wlink flow-control health registers.

    Returns::

        {"nodes": {"AW": {"crc": 0, "acknack": 0x1, "empty": 1, "full": 0,
                          "halffull": 0, "txfifo_empty": 1, "axi_data": True},
                   ...},
         "worst_crc": 0,
         "stuck": [],            # AXI data nodes with a non-empty Ack/Nack FIFO
         "crc_total": 0,
         "ok": True}

    Register layout from kr260_eth_xfer.py:84-88 (REGISTER_MAP.md §2.3): node
    bases are TLAPB-relative, per-node registers at +0x08 (TX FIFO empty),
    +0x10 (Ack/Nack FIFO flags) and +0x20 (16-bit CRC error count).
    """
    nodes: Dict[str, Dict[str, Any]] = {}
    stuck: List[str] = []
    worst_crc = 0
    crc_total = 0

    for name, base in regs.FC_NODES:
        txfifo = board.reg_read(base + regs.FC_TXFIFO)
        acknack = board.reg_read(base + regs.FC_ACKNACK)
        crc = board.reg_read(base + regs.FC_CRC) & 0xFFFF
        empty = acknack & 1
        is_axi = name in regs.FC_AXI_DATA_NODES
        nodes[name] = {
            "base": base,
            "crc": crc,
            "acknack": acknack,
            "empty": empty,
            "full": (acknack >> 1) & 1,
            "halffull": (acknack >> 2) & 1,
            "almostempty": (acknack >> 3) & 1,
            "almostfull": (acknack >> 4) & 1,
            "txfifo_empty": txfifo & 1,
            "axi_data": is_axi,
        }
        worst_crc = max(worst_crc, crc)
        crc_total += crc
        # A non-empty Ack/Nack FIFO on an AXI data node is the credit/ACK-stall
        # signature. On GenBus/TideLink it is not — those keep the recovery logic.
        if is_axi and not empty:
            stuck.append(name)

    return {
        "nodes": nodes,
        "worst_crc": worst_crc,
        "crc_total": crc_total,
        "stuck": stuck,
        "ok": not stuck and worst_crc == 0,
    }


def link_health(board: Any) -> Dict[str, Any]:
    """Full read-only health sample for one board: link, credits, faults, FC nodes.

    This is what ``ChipletPair.health(board)`` returns and what the ``hetsoc
    health`` CLI prints. Everything read here is RO and in-window.
    """
    lane = board.lane_status()
    status_raw = board.reg_read(regs.STATUS)
    sticky = regs.decode_sticky_status(status_raw)
    credit = board.reg_read(regs.CREDIT_COUNT) & regs.CREDIT_COUNT_MASK
    obs_credit = board.reg_read(regs.OBS_FC_CREDIT)
    sync_det = board.reg_read(regs.SYNC_DET)
    wlink_status = board.reg_read(regs.WLINK_LINK_STATUS)
    fc = fc_health(board)

    sample: Dict[str, Any] = {
        "board": getattr(board, "name", "?"),
        "role": getattr(board, "role", "?"),
        "target": getattr(getattr(board, "target", None), "name", "?"),
        "lane_status": lane.as_dict(),
        "fcsm": lane.fcsm,
        "fcsm_name": lane.fcsm_name,
        "cal_done": lane.cal_done,
        "link_up": lane.link_up,
        "lane_fault": lane.lane_fault,
        # Sticky faults [3:1]: OVERRUN / UNDERRUN / MASTER_ERROR. Any set is a
        # latched fault since the last reset, not a transient.
        "status_raw": status_raw,
        "sticky": sticky["sticky"],
        "sticky_bits": {k: v for k, v in sticky.items()
                        if k in ("overrun", "underrun", "master_error")},
        # SIDEBAND ONLY — see the module banner. Reads 4096 (idle FIFO) during a
        # healthy peer-window soak because the peer window does not use this path.
        "credit_count": credit,
        "obs_fc_credit": obs_credit,
        # Drift indicators: a climbing sync_detected_cnt or a non-zero lane_fault
        # points at the marginal eye / one-shot calibration.
        "sync_detected": (sync_det >> 16) & 0xFFFF,
        "wlink_status": wlink_status,
        "tx_lanes_active": int(bool(wlink_status & regs.WLINK_TX_LANES_ACTIVE)),
        "rx_data_valid": int(bool(wlink_status & regs.WLINK_RX_DATA_VALID)),
        "fc": fc,
    }
    sample["verdict"] = verdict(sample)
    return sample


def verdict(sample: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a health sample into ``{"ok": bool, "reasons": [...]}``.

    ``ok`` means "nothing observable says this link is degrading". It is NOT a
    promise that the next cross-die transfer will complete — the wedge is
    intermittent and the eye is marginal by design.
    """
    reasons: List[str] = []
    if not sample.get("link_up"):
        reasons.append("link DOWN (fcsm=%s cal_done=%s; need fcsm=4, cal_done=1)"
                       % (sample.get("fcsm"), sample.get("cal_done")))
    if sample.get("sticky"):
        bits = ", ".join(k for k, v in sample.get("sticky_bits", {}).items() if v)
        reasons.append("sticky fault(s) latched: %s (STATUS=0x%08X)"
                       % (bits or "unknown", sample.get("status_raw", 0)))
    fc = sample.get("fc", {})
    if fc.get("stuck"):
        reasons.append(
            "AXI data node(s) %s have a NON-EMPTY Ack/Nack FIFO — the credit/ACK "
            "stall signature. These nodes ship the recovery-stripped upstream "
            "FCSM, so this does not self-clear; stop cross-die traffic"
            % ",".join(fc["stuck"]))
    if fc.get("worst_crc"):
        reasons.append(
            "CRC errors on the FC nodes (worst=%d) — bit errors from calibration "
            "drift / a marginal eye. Calibration is one-shot (latched at "
            "bring-up), so this only gets worse within a session"
            % fc["worst_crc"])
    if sample.get("lane_fault"):
        reasons.append("lane_fault=0x%02X" % sample["lane_fault"])
    return {"ok": not reasons, "reasons": reasons}


def compare_fc_health(before: Dict[str, Any], after: Dict[str, Any]
                      ) -> Dict[str, Any]:
    """Delta two ``fc_health`` samples — the between-transfers poll.

    A *rising* CRC count is the actionable signal; an absolute count from a
    previous session is not.
    """
    deltas: Dict[str, int] = {}
    for name, node in after.get("nodes", {}).items():
        prev = before.get("nodes", {}).get(name, {})
        delta = node.get("crc", 0) - prev.get("crc", 0)
        if delta:
            deltas[name] = delta
    newly_stuck = [n for n in after.get("stuck", [])
                   if n not in before.get("stuck", [])]
    return {
        "crc_delta": deltas,
        "newly_stuck": newly_stuck,
        "degrading": bool(deltas or newly_stuck),
    }


def format_health(sample: Dict[str, Any], prefix: str = "  ") -> str:
    """Human-readable health dump — the ``hetsoc health`` output."""
    lines = [
        "%sboard=%s role=%s target=%s"
        % (prefix, sample.get("board"), sample.get("role"), sample.get("target")),
        "%sSWI_LANE_STATUS=0x%08X  fcsm=%d (%s)  cal_done=%d  lane_fault=0x%02X"
        "  -> link %s"
        % (prefix, sample["lane_status"]["raw"], sample["fcsm"],
           sample["fcsm_name"], sample["cal_done"], sample["lane_fault"],
           "UP" if sample["link_up"] else "DOWN"),
        "%sSTATUS=0x%08X sticky[3:1]=0x%X   CREDIT_COUNT=%d   OBS_FC_CREDIT=0x%08X"
        % (prefix, sample["status_raw"], sample["sticky"],
           sample["credit_count"], sample["obs_fc_credit"]),
        "%s  (CREDIT_COUNT/OBS_FC_CREDIT are the SIDEBAND node — they do NOT see "
        "the AXI nodes that wedge)" % prefix,
        "%ssync_detected=%d  tx_lanes_active=%d  rx_data_valid=%d"
        % (prefix, sample["sync_detected"], sample["tx_lanes_active"],
           sample["rx_data_valid"]),
        "%sper-node Wlink FC health (AXI data nodes are the recovery-stripped ones):"
        % prefix,
        "%s  node      CRC_errs  AckNack E/F/HF  TXfifo_empty" % prefix,
    ]
    for name, _base in regs.FC_NODES:
        node = sample["fc"]["nodes"].get(name)
        if node is None:
            continue
        lines.append("%s  %-8s  %6d    %d/%d/%d           %d%s"
                     % (prefix, name, node["crc"], node["empty"], node["full"],
                        node["halffull"], node["txfifo_empty"],
                        "   <- AXI data" if node["axi_data"] else ""))
    lines.append("%sworst CRC=%d; AXI nodes with non-empty Ack/Nack FIFO: %s"
                 % (prefix, sample["fc"]["worst_crc"],
                    ",".join(sample["fc"]["stuck"]) or "none"))
    result = sample.get("verdict", {})
    lines.append("%sVERDICT: %s" % (prefix, "HEALTHY" if result.get("ok") else "DEGRADED"))
    for reason in result.get("reasons", []):
        lines.append("%s  - %s" % (prefix, reason))
    return "\n".join(lines)


def sample_between_transfers(board: Any, previous: Optional[Dict[str, Any]] = None
                             ) -> Dict[str, Any]:
    """Take an FC sample and delta it against *previous* (the soak-loop helper).

    This is fix #2 from docs/CROSS_DIE_WEDGE_ROOTCAUSE.md's fix path — the host
    interim that needs no rebuild: poll the per-node registers between transfers
    and stop on a stuck FIFO / rising CRC rather than letting the next
    transaction wedge the bus.
    """
    now = fc_health(board)
    if previous is None:
        return {"fc": now, "delta": None, "degrading": False}
    delta = compare_fc_health(previous, now)
    if delta["degrading"]:
        _log.warning("FC health degrading between transfers",
                     board=getattr(board, "name", "?"),
                     crc_delta=str(delta["crc_delta"]),
                     newly_stuck=",".join(delta["newly_stuck"]) or "none")
    return {"fc": now, "delta": delta, "degrading": delta["degrading"]}
