#!/usr/bin/env python3
# =============================================================================
# hetsoc_agent — the on-board half of the transport. Runs as root on the KR260.
#
# WHY IT EXISTS
# -------------
# The proven bench scripts pay a full ssh round-trip per 32-bit access
# (kr260_eth_run.sh invokes a fresh `sudo python3 …` per operation). That is
# ~200 ms per poke: fine for a status dump, hopeless for a 2000-beat soak, and
# it means the FC-health poll that catches the known wedge cannot run BETWEEN
# transfers at any useful rate. This agent is started ONCE over a persistent SSH
# channel and then answers one line per access over the already-open pipe.
#
# WHY IT RE-CHECKS THE WINDOW
# ---------------------------
# Defence in depth. The host-side `Target.to_host()` is the primary guard, but a
# bug there would poke an arbitrary PS physical address, and on this board a PS
# access to an undecoded PL address hangs the ZynqMP AXI bus with no timeout
# (JTAG POR to recover). So the agent is told its window at startup and refuses
# ANY address outside it — an out-of-window request is answered with an error
# line, never with an mmap. Two independent guards, one hazard.
#
# WHY IT IS A SEPARATE, DEPENDENCY-FREE FILE
# ------------------------------------------
# It is copied verbatim to the board and run by the stock `python3` of a plain
# Ubuntu image: stdlib only, no hetsoc import, Python 3.6+ syntax.
#
# PROTOCOL (one request per line on stdin, one response per line on stdout)
#   p                      -> pong <version> <pid>
#   r <phys>               -> = <value>
#   w <phys> <value>       -> ok
#   m <phys> <count>       -> = <v0> <v1> ...        (count consecutive words)
#   f <phys> <count> <val> -> ok                     (fill: count words)
#   win                    -> = <base> <size>
#   q                      -> bye, then exit
#   anything else / error  -> ! <message>
# All numbers are hex with a 0x prefix. A response line is ALWAYS produced,
# except when the bus itself hangs — which is precisely the case the host's
# `guarded()` timeout turns into WedgeDetected.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""Line-protocol /dev/mem agent executed on the board. Stdlib only."""

from __future__ import print_function

import mmap
import os
import struct
import sys

AGENT_VERSION = "1"
PAGE_SIZE = mmap.PAGESIZE
_WORD = struct.Struct("<I")


class WindowError(Exception):
    """Requested address is outside the window this agent was started with."""


class Agent(object):
    """Page-cached /dev/mem accessor bounded to one PS physical window."""

    def __init__(self, window_base, window_size, dev="/dev/mem"):
        if window_size <= 0:
            raise ValueError("window_size must be > 0 (got 0x%X)" % window_size)
        self.window_base = window_base
        self.window_size = window_size
        self.dev = dev
        self._fd = None
        self._pages = {}          # page phys addr -> mmap

    # -- window guard -------------------------------------------------- #
    def _check(self, phys, nwords=1):
        end = phys + 4 * nwords
        if phys < self.window_base or end > self.window_base + self.window_size:
            raise WindowError(
                "0x%X..0x%X is outside this agent's window 0x%X..0x%X. "
                "Refusing: a PS access to a PL address the SoC does not decode "
                "hangs the AXI bus with no timeout (JTAG POR to recover)."
                % (phys, end - 1, self.window_base,
                   self.window_base + self.window_size - 1))
        if phys & 3:
            raise WindowError("0x%X is not 4-byte aligned" % phys)

    # -- mmap plumbing -------------------------------------------------- #
    def _page(self, phys):
        page = phys & ~(PAGE_SIZE - 1)
        got = self._pages.get(page)
        if got is None:
            if self._fd is None:
                try:
                    self._fd = open(self.dev, "r+b", buffering=0)
                except Exception as exc:                # noqa: BLE001
                    raise WindowError(
                        "open(%s) failed: %s — the agent must run as root."
                        % (self.dev, exc))
            try:
                got = mmap.mmap(self._fd.fileno(), PAGE_SIZE, mmap.MAP_SHARED,
                                mmap.PROT_READ | mmap.PROT_WRITE, offset=page)
            except Exception as exc:                    # noqa: BLE001
                raise WindowError(
                    "mmap %s @ 0x%X failed: %s (run as root; is the bitstream "
                    "loaded? /sys/class/fpga_manager/fpga0/state should read "
                    "'operating')" % (self.dev, page, exc))
            self._pages[page] = got
        return got, phys - page

    # -- operations ----------------------------------------------------- #
    def read(self, phys):
        self._check(phys)
        page, off = self._page(phys)
        return _WORD.unpack(page[off:off + 4])[0]

    def write(self, phys, value):
        self._check(phys)
        page, off = self._page(phys)
        page[off:off + 4] = _WORD.pack(value & 0xFFFFFFFF)

    def read_many(self, phys, count):
        self._check(phys, count)
        return [self.read(phys + 4 * i) for i in range(count)]

    def fill(self, phys, count, value):
        self._check(phys, count)
        for i in range(count):
            self.write(phys + 4 * i, value)

    def close(self):
        for page in self._pages.values():
            try:
                page.close()
            except Exception:                            # noqa: BLE001
                pass
        self._pages = {}
        if self._fd is not None:
            try:
                self._fd.close()
            finally:
                self._fd = None


def _int(text):
    return int(text, 0)


def serve(agent, stdin=None, stdout=None):
    """Run the request loop until EOF or ``q``. Returns the exit status."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout

    def reply(line):
        stdout.write(line + "\n")
        stdout.flush()

    reply("ready %s %d 0x%X 0x%X"
          % (AGENT_VERSION, os.getpid(), agent.window_base, agent.window_size))
    for raw in stdin:
        parts = raw.split()
        if not parts:
            continue
        op = parts[0]
        try:
            if op == "q":
                reply("bye")
                return 0
            if op == "p":
                reply("pong %s %d" % (AGENT_VERSION, os.getpid()))
            elif op == "win":
                reply("= 0x%X 0x%X" % (agent.window_base, agent.window_size))
            elif op == "r":
                reply("= 0x%08X" % agent.read(_int(parts[1])))
            elif op == "w":
                agent.write(_int(parts[1]), _int(parts[2]))
                reply("ok")
            elif op == "m":
                vals = agent.read_many(_int(parts[1]), _int(parts[2]))
                reply("= " + " ".join("0x%08X" % v for v in vals))
            elif op == "f":
                agent.fill(_int(parts[1]), _int(parts[2]), _int(parts[3]))
                reply("ok")
            else:
                reply("! unknown op %r (want p|r|w|m|f|win|q)" % op)
        except (IndexError, ValueError) as exc:
            reply("! bad request %r: %s" % (raw.strip(), exc))
        except WindowError as exc:
            reply("! %s" % exc)
        except Exception as exc:                        # noqa: BLE001
            reply("! %s: %s" % (type(exc).__name__, exc))
    return 0


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="hetsoc on-board /dev/mem agent (line protocol on stdio).")
    parser.add_argument("--window-base", required=True,
                        help="PS physical base of the SoC backdoor window.")
    parser.add_argument("--window-size", required=True,
                        help="Size of that window in bytes.")
    parser.add_argument("--dev", default="/dev/mem")
    args = parser.parse_args(argv)

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("! hetsoc agent needs root for /dev/mem (run under sudo).",
              file=sys.stderr)
        return 4
    try:
        agent = Agent(_int(args.window_base), _int(args.window_size), args.dev)
    except ValueError as exc:
        print("! %s" % exc, file=sys.stderr)
        return 4
    try:
        return serve(agent)
    finally:
        agent.close()


if __name__ == "__main__":
    sys.exit(main())
