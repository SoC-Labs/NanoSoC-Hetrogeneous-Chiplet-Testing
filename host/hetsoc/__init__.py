# =============================================================================
# hetsoc — host test framework for the NanoSoC heterogeneous chiplet pair.
#
# The layer every on-silicon test drives: two KR260 boards, each running one
# chiplet bitstream, joined by a J21 ribbon carrying the TideLink die-to-die
# interface. It generalises the ad-hoc bench scripts that proved the homogeneous
# eth<->eth pair on silicon (2026-07-27..29) into a design-agnostic framework
# whose SAFETY RULES ARE CODE, not runbook prose:
#
#   * every address passes Target.to_host() or it never reaches /dev/mem;
#   * every peer-aperture access is gated on FCSM==4;
#   * every board operation is timeout-bounded — a hang raises WedgeDetected;
#   * the registry is structurally incapable of emitting a bare-link address
#     for a chiplet target.
#
# IMPORT DISCIPLINE: importing `hetsoc` must work on any machine with no board,
# no network, no /dev/mem and no third-party packages — that is what lets the L0
# test tier run in any CI container. Everything that can touch hardware
# (`board`, `pair`, `fpgahub`, `health`, `transport`) is behind the lazy
# `__getattr__` below and pulls in `subprocess` only when first used.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""Host test framework for the NanoSoC heterogeneous (eth <-> compute) chiplet pair."""

from __future__ import annotations

from typing import Any

__version__ = "0.1.0"

# --- always safe to import: pure host logic, no hardware, no deps ------------
from . import regs, safety, targets                      # noqa: F401
from .regs import (FCSM_LINK_IDLE, LaneStatus, cam_rule,  # noqa: F401
                   decode_lane_status)
from .safety import (AddressGuardError, ConfigError,      # noqa: F401
                     HetsocError, LinkDownError, ProvisionalTargetError,
                     TransportError, WedgeDetected, guarded, require_link_up,
                     run_guarded)
from .targets import TARGETS, Target, get_target          # noqa: F401

# --- lazy: anything that could reach a board --------------------------------
_LAZY = {
    "Board": ("hetsoc.board", "Board"),
    "ChipletPair": ("hetsoc.pair", "ChipletPair"),
    "board": ("hetsoc.board", None),
    "pair": ("hetsoc.pair", None),
    "fpgahub": ("hetsoc.fpgahub", None),
    "health": ("hetsoc.health", None),
    "transport": ("hetsoc.transport", None),
    "config": ("hetsoc.config", None),
    "cli": ("hetsoc.cli", None),
    "load_config": ("hetsoc.config", "load"),
    "MemoryTransport": ("hetsoc.transport", "MemoryTransport"),
    "lease": ("hetsoc.fpgahub", "lease"),
}

__all__ = [
    "__version__",
    # safety
    "AddressGuardError", "ProvisionalTargetError", "LinkDownError",
    "WedgeDetected", "ConfigError", "TransportError", "HetsocError",
    "safety", "require_link_up", "guarded", "run_guarded",
    # regs
    "regs", "cam_rule", "decode_lane_status", "LaneStatus", "FCSM_LINK_IDLE",
    # targets
    "targets", "Target", "TARGETS", "get_target",
    # lazy
    "Board", "ChipletPair", "MemoryTransport", "fpgahub", "health", "transport",
    "config", "load_config", "lease",
]


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute access — keeps hardware imports out of L0."""
    entry = _LAZY.get(name)
    if entry is None:
        raise AttributeError("module 'hetsoc' has no attribute %r" % name)
    import importlib

    module = importlib.import_module(entry[0])
    return module if entry[1] is None else getattr(module, entry[1])


def __dir__():                                              # pragma: no cover
    return sorted(set(list(globals()) + list(_LAZY)))
