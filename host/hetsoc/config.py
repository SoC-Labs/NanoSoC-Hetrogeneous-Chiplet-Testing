# =============================================================================
# hetsoc.config — where the bench description lives.
#
# WHY A FILE AND NOT ENV VARS: the two things that can wedge a board are the
# *address window* and *which image is on which board*. Both belong in a
# reviewable file next to a citation, not in a shell export that one operator
# set three sessions ago. This mirrors the fpga/fpgahub.toml per-project
# manifest precedent that CHIPLET_HOST_TOOLING_PLAN.md points at.
#
# SEARCH ORDER (first hit wins, no merging — a half-merged address map is worse
# than a missing one):
#     $HETSOC_CONFIG   ->   ./hetsoc.toml   ->   ~/.config/hetsoc.toml
#
# The `[target.*]` section is the per-design override the tooling plan calls for
# and is the ONLY supported way to give a provisional target (the compute
# chiplet) a real window base — see hetsoc.targets.target_from_dict, which
# refuses to de-provisionalise a target whose `source` still says TBD.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""Load the bench description (boards, pairs, target overrides) from TOML."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from . import _toml
from .log import get_logger
from .safety import ConfigError
from .targets import TARGETS, Target, get_target, target_from_dict

__all__ = ["Config", "load", "config_search_path", "DEFAULT_FILENAME"]

_log = get_logger("config")

DEFAULT_FILENAME = "hetsoc.toml"

_BOARD_KEYS = {"host", "fpgahub", "target", "role", "password", "proxy",
               "transport", "deploy_command", "timeout_s"}
_PAIR_KEYS = {"a", "b", "description"}


def config_search_path() -> List[str]:
    """The paths ``load()`` considers, in order."""
    paths = []
    override = os.environ.get("HETSOC_CONFIG")
    if override:
        paths.append(override)
    paths.append(os.path.join(os.getcwd(), DEFAULT_FILENAME))
    paths.append(os.path.expanduser("~/.config/hetsoc.toml"))
    return paths


class Config:
    """Parsed bench description, and the factory for Boards and Pairs."""

    def __init__(self, data: Dict[str, Any], path: Optional[str] = None,
                 register_targets: bool = True) -> None:
        self.path = path
        self.raw = dict(data)
        self.boards: Dict[str, Dict[str, Any]] = dict(data.get("board", {}) or {})
        self.pairs: Dict[str, Dict[str, Any]] = dict(data.get("pair", {}) or {})
        self.targets: Dict[str, Target] = {}
        self.defaults: Dict[str, Any] = dict(data.get("defaults", {}) or {})

        unknown_sections = set(self.raw) - {"board", "pair", "target", "defaults"}
        if unknown_sections:
            raise ConfigError(
                "%s: unknown top-level section(s): %s. Known: [board.*], "
                "[pair.*], [target.*], [defaults]."
                % (path or "<config>", ", ".join(sorted(unknown_sections))))

        # Target overrides FIRST — a board may reference an overridden target.
        for name, table in dict(data.get("target", {}) or {}).items():
            target = target_from_dict(str(name), dict(table))
            self.targets[str(name)] = target
            if register_targets:
                TARGETS[str(name)] = target
                _log.info("target override applied", target=name,
                          window=target.window_base, size=target.window_size,
                          provisional=target.provisional)

        self._validate()

    # ------------------------------------------------------------------ #
    def _validate(self) -> None:
        where = self.path or "<config>"
        for name, spec in self.boards.items():
            unknown = set(spec) - _BOARD_KEYS
            if unknown:
                raise ConfigError("%s: [board.%s] unknown key(s): %s. Known: %s"
                                  % (where, name, ", ".join(sorted(unknown)),
                                     ", ".join(sorted(_BOARD_KEYS))))
            for required in ("host", "target", "role"):
                if not spec.get(required):
                    raise ConfigError(
                        "%s: [board.%s] is missing `%s`. A board needs at least "
                        "host, target and role — the role decides which image "
                        "(die_a master / die_b slave) belongs on it, and getting "
                        "that wrong drives two outputs onto every ribbon lane."
                        % (where, name, required))
            try:
                get_target(spec["target"])
            except KeyError as exc:
                raise ConfigError("%s: [board.%s] %s" % (where, name, exc))
        for name, spec in self.pairs.items():
            unknown = set(spec) - _PAIR_KEYS
            if unknown:
                raise ConfigError("%s: [pair.%s] unknown key(s): %s"
                                  % (where, name, ", ".join(sorted(unknown))))
            for side in ("a", "b"):
                board_name = spec.get(side)
                if not board_name:
                    raise ConfigError("%s: [pair.%s] is missing `%s`"
                                      % (where, name, side))
                if board_name not in self.boards:
                    raise ConfigError(
                        "%s: [pair.%s].%s names board %r, which has no "
                        "[board.%s] section. Known boards: %s"
                        % (where, name, side, board_name, board_name,
                           ", ".join(sorted(self.boards)) or "(none)"))

    # ------------------------------------------------------------------ #
    def board_names(self) -> List[str]:
        return sorted(self.boards)

    def pair_names(self) -> List[str]:
        return sorted(self.pairs)

    def board(self, name: str, **overrides: Any):
        """Build a :class:`~hetsoc.board.Board` from ``[board.<name>]``."""
        from .board import Board                     # lazy: keeps L0 import-light

        if name not in self.boards:
            raise ConfigError(
                "no [board.%s] in %s. Known boards: %s"
                % (name, self.path or "<config>",
                   ", ".join(self.board_names()) or "(none)"))
        spec = dict(self.defaults)
        spec.update(self.boards[name])
        spec.update(overrides)
        return Board(
            host=spec["host"],
            target=spec["target"],
            role=spec["role"],
            name=spec.get("name", name),
            fpgahub=spec.get("fpgahub"),
            transport_kind=spec.get("transport"),
            password=spec.get("password"),
            proxy=spec.get("proxy"),
            deploy_command=spec.get("deploy_command"),
            timeout_s=float(spec.get("timeout_s", 20.0)),
        )

    def pair(self, name: str = "default", **overrides: Any):
        """Build a :class:`~hetsoc.pair.ChipletPair` from ``[pair.<name>]``."""
        from .pair import ChipletPair                # lazy

        if name not in self.pairs:
            raise ConfigError(
                "no [pair.%s] in %s. Known pairs: %s"
                % (name, self.path or "<config>",
                   ", ".join(self.pair_names()) or "(none)"))
        spec = self.pairs[name]
        return ChipletPair(self.board(spec["a"], **overrides),
                           self.board(spec["b"], **overrides))

    def describe(self) -> str:
        lines = ["config: %s (TOML backend: %s)"
                 % (self.path or "<defaults>", _toml.backend())]
        for name in self.board_names():
            spec = self.boards[name]
            lines.append("  board %-10s host=%-24s target=%-22s role=%-6s fpgahub=%s"
                         % (name, spec.get("host"), spec.get("target"),
                            spec.get("role"), spec.get("fpgahub", "-")))
        for name in self.pair_names():
            spec = self.pairs[name]
            lines.append("  pair  %-10s a=%s b=%s" % (name, spec.get("a"),
                                                      spec.get("b")))
        for name in sorted(self.targets):
            lines.append("  target override: %s" % name)
        return "\n".join(lines)


def load(path: Optional[str] = None, register_targets: bool = True) -> Config:
    """Load the bench description.

    Search order: *path* if given, else ``$HETSOC_CONFIG``, ``./hetsoc.toml``,
    ``~/.config/hetsoc.toml``. First hit wins — configurations are NOT merged,
    because a half-merged address map is more dangerous than a missing one.

    Raises:
        ConfigError: nothing found, or the file is malformed.
    """
    candidates = [path] if path else config_search_path()
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            try:
                data = _toml.load(candidate)
            except Exception as exc:                 # noqa: BLE001
                raise ConfigError("could not parse %s: %s" % (candidate, exc))
            _log.debug("config loaded", path=candidate, backend=_toml.backend())
            return Config(data, candidate, register_targets=register_targets)
    raise ConfigError(
        "no hetsoc config found. Looked at:\n  %s\n"
        "Copy `hetsoc.toml.example` from the repo root to ./hetsoc.toml and edit "
        "the two board addresses, or point $HETSOC_CONFIG at one."
        % "\n  ".join(c for c in candidates if c))


def load_optional(path: Optional[str] = None) -> Optional[Config]:
    """``load()`` but returns None instead of raising when nothing is found."""
    try:
        return load(path)
    except ConfigError:
        return None
