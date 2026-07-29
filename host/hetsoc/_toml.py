# =============================================================================
# hetsoc._toml — TOML loading with a zero-dependency fallback.
#
# WHY A FALLBACK PARSER: `tomllib` only landed in Python 3.11, and the boards run
# plain Ubuntu with whatever python3 the image ships (3.8 on the current dev
# host). L0 must import and run with NO third-party packages — that is the
# property that lets the offline test tier run in any CI container. So: use
# `tomllib` if present, else `tomli` if installed, else parse the small subset
# the hetsoc schema actually uses.
#
# THE SUBSET: `[table]` / `[a.b]` headers, `key = value` pairs, and values that
# are strings, integers (dec/hex/oct/bin, with `_` separators), floats, booleans
# or single-line arrays. That covers every construct in hetsoc.toml.example. Any
# construct outside it raises rather than being silently mis-read — a mis-parsed
# `window_base` is a wedge, so guessing is not an option.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""TOML loading: stdlib tomllib, else tomli, else a documented small subset."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

__all__ = ["loads", "load", "backend"]

try:                                                # Python >= 3.11
    import tomllib as _toml                         # type: ignore
    _BACKEND = "tomllib"
except ImportError:                                 # pragma: no cover - env dep
    try:
        import tomli as _toml                       # type: ignore
        _BACKEND = "tomli"
    except ImportError:
        _toml = None                                # type: ignore
        _BACKEND = "builtin-subset"


def backend() -> str:
    """Which parser is in use — reported by ``hetsoc config`` for debugging."""
    return _BACKEND


class TomlSubsetError(ValueError):
    """The builtin fallback met a construct outside the supported subset."""


_HEADER = re.compile(r"^\[([A-Za-z0-9_.\-\"' ]+)\]$")
_PAIR = re.compile(r"^([A-Za-z0-9_\-\"']+)\s*=\s*(.+)$")
_INT = re.compile(r"^[+-]?(0[xX][0-9A-Fa-f_]+|0[oO][0-7_]+|0[bB][01_]+|[0-9][0-9_]*)$")
_FLOAT = re.compile(r"^[+-]?[0-9][0-9_]*\.[0-9_]+([eE][+-]?[0-9]+)?$")


def _strip_comment(line: str) -> str:
    """Remove a trailing `#` comment that is not inside a string."""
    out: List[str] = []
    quote = ""
    for char in line:
        if quote:
            out.append(char)
            if char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
            out.append(char)
            continue
        if char == "#":
            break
        out.append(char)
    return "".join(out).strip()


def _unquote(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _split_array(body: str) -> List[str]:
    items: List[str] = []
    depth = 0
    quote = ""
    current: List[str] = []
    for char in body:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
        elif char == "[":
            depth += 1
            current.append(char)
        elif char == "]":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(char)
    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return [i.strip() for i in items if i.strip()]


def _value(token: str, where: str) -> Any:
    token = token.strip()
    if not token:
        raise TomlSubsetError("%s: empty value" % where)
    if token.startswith("["):
        if not token.endswith("]"):
            raise TomlSubsetError(
                "%s: multi-line arrays are outside the supported TOML subset. "
                "Put the array on one line, or install `tomli`." % where)
        return [_value(item, where) for item in _split_array(token[1:-1])]
    if token in ("true", "false"):
        return token == "true"
    if token[0] in ("'", '"'):
        return _unquote(token)
    if _INT.match(token):
        return int(token.replace("_", ""), 0)
    if _FLOAT.match(token):
        return float(token.replace("_", ""))
    if token.startswith("{"):
        raise TomlSubsetError(
            "%s: inline tables are outside the supported TOML subset. Use a "
            "[section] header, or install `tomli`." % where)
    raise TomlSubsetError(
        "%s: cannot parse value %r with the builtin TOML subset. Supported: "
        "strings, integers (0x… allowed), floats, true/false, single-line "
        "arrays. Install `tomli` for full TOML." % (where, token))


def _descend(root: Dict[str, Any], parts: Tuple[str, ...]) -> Dict[str, Any]:
    node = root
    for part in parts:
        child = node.setdefault(part, {})
        if not isinstance(child, dict):
            raise TomlSubsetError("table %r collides with a value" % ".".join(parts))
        node = child
    return node


def _loads_subset(text: str) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    table = root
    for number, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line:
            continue
        where = "line %d" % number
        header = _HEADER.match(line)
        if header:
            if line.startswith("[["):
                raise TomlSubsetError(
                    "%s: arrays of tables are outside the supported TOML subset."
                    % where)
            parts = tuple(_unquote(p) for p in header.group(1).split("."))
            table = _descend(root, parts)
            continue
        pair = _PAIR.match(line)
        if not pair:
            raise TomlSubsetError("%s: cannot parse %r" % (where, raw.strip()))
        table[_unquote(pair.group(1))] = _value(pair.group(2), where)
    return root


def loads(text: str) -> Dict[str, Any]:
    """Parse TOML text into nested dicts."""
    if _toml is not None:
        return _toml.loads(text)
    return _loads_subset(text)


def load(path: str) -> Dict[str, Any]:
    """Parse a TOML file."""
    with open(path, "r") as handle:
        return loads(handle.read())
