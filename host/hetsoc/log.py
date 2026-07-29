# =============================================================================
# hetsoc.log — structured logging.
#
# WHY STRUCTURED: a wedge is diagnosed after the fact, from whatever the run
# printed before it stopped. "read failed" is useless; "board=eth op=read
# soc=0x2E032108 host=0x42E032108 fcsm=1" tells you which access, on which
# board, at which address, with the link in which state. Every board-touching
# call logs the SoC address AND the translated PS physical address, because the
# translation is exactly where a wedge-causing mistake hides.
#
# Kept dependency-free (stdlib logging) so L0 imports anywhere.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""Structured logging helpers for the hetsoc framework."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

__all__ = ["get_logger", "configure", "HetsocLogger"]

_ROOT_NAME = "hetsoc"
_configured = False


def _fmt_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        # Addresses and register values read far better in hex, and every large
        # int in this framework IS an address or a register value.
        return "0x%X" % value if abs(value) >= 0x1000 else str(value)
    text = str(value)
    return '"%s"' % text if (" " in text or "=" in text) else text


class _KeyValueFormatter(logging.Formatter):
    """`level logger message key=value key=value` — greppable, no deps."""

    def format(self, record: logging.LogRecord) -> str:
        base = "%-7s %-18s %s" % (record.levelname, record.name,
                                  record.getMessage())
        fields = getattr(record, "hetsoc_fields", None)
        if fields:
            base += "  " + " ".join("%s=%s" % (k, _fmt_value(v))
                                    for k, v in fields.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class HetsocLogger:
    """Thin wrapper adding ``key=value`` context to every record.

    ``log.info("peer write", board=b.name, soc=addr, host=phys)``
    """

    __slots__ = ("_logger", "_context")

    def __init__(self, logger: logging.Logger, **context: Any) -> None:
        self._logger = logger
        self._context = dict(context)

    def bind(self, **context: Any) -> "HetsocLogger":
        """Return a child logger with extra permanent context (e.g. board name)."""
        merged = dict(self._context)
        merged.update(context)
        return HetsocLogger(self._logger, **merged)

    def _log(self, level: int, msg: str, *args: Any, **fields: Any) -> None:
        if not self._logger.isEnabledFor(level):
            return
        merged = dict(self._context)
        exc_info = fields.pop("exc_info", None)
        merged.update(fields)
        self._logger.log(level, msg, *args,
                         extra={"hetsoc_fields": merged}, exc_info=exc_info)

    def debug(self, msg: str, *a: Any, **kw: Any) -> None:
        self._log(logging.DEBUG, msg, *a, **kw)

    def info(self, msg: str, *a: Any, **kw: Any) -> None:
        self._log(logging.INFO, msg, *a, **kw)

    def warning(self, msg: str, *a: Any, **kw: Any) -> None:
        self._log(logging.WARNING, msg, *a, **kw)

    def error(self, msg: str, *a: Any, **kw: Any) -> None:
        self._log(logging.ERROR, msg, *a, **kw)

    def critical(self, msg: str, *a: Any, **kw: Any) -> None:
        self._log(logging.CRITICAL, msg, *a, **kw)

    @property
    def raw(self) -> logging.Logger:
        return self._logger


def configure(level: Optional[str] = None, stream: Any = None) -> None:
    """Install the hetsoc handler once. ``HETSOC_LOG`` overrides the level.

    Never touches the root logger — a consumer (pytest, a CI harness) keeps its
    own configuration.
    """
    global _configured
    logger = logging.getLogger(_ROOT_NAME)
    if not _configured:
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(_KeyValueFormatter())
        logger.addHandler(handler)
        logger.propagate = False
        _configured = True
    resolved = (level or os.environ.get("HETSOC_LOG") or "INFO").upper()
    logger.setLevel(getattr(logging, resolved, logging.INFO))


def get_logger(name: str = "", **context: Any) -> HetsocLogger:
    """Get a structured logger, e.g. ``get_logger("board", board="eth")``."""
    full = _ROOT_NAME if not name else "%s.%s" % (_ROOT_NAME, name)
    return HetsocLogger(logging.getLogger(full), **context)
