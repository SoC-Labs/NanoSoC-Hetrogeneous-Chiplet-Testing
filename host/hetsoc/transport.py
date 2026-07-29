# =============================================================================
# hetsoc.transport — how a 32-bit access actually reaches the board.
#
# THREE IMPLEMENTATIONS, ONE INTERFACE
# ------------------------------------
#   MemoryTransport      pure host dict. No board, no network, no /dev/mem.
#                        This is what makes every L0 test runnable anywhere,
#                        and it enforces the SAME window bound the real agent
#                        does, so an out-of-window bug fails offline too.
#   SshAgentTransport    ONE persistent SSH connection + one long-lived on-board
#                        agent (hetsoc.agent). One line per access over an
#                        already-open pipe.  <- default
#   SshOneShotTransport  one ssh per batch. Byte-for-byte the auth/sudo shape of
#                        kr260_eth_run.sh. Slow but has no moving parts; the
#                        fallback when ControlMaster or the agent misbehaves.
#
# WHY THE PERSISTENT ONE MATTERS
# ------------------------------
# The existing scripts pay a full ssh round-trip per 32-bit operation. A soak of
# 2000 beats is then ~7 minutes of ssh, and — worse — the per-node FC-health
# poll that is the ONLY visibility into the known cross-die wedge
# (docs/CROSS_DIE_WEDGE_ROOTCAUSE.md: OBS_FC_CREDIT and SWI_LANE_STATUS do NOT
# see the AXI nodes that wedge) cannot be run between transfers at any useful
# rate. 21 register reads per health sample is 4 s of ssh versus ~20 ms here.
#
# AUTH — MIRRORS kr260_eth_run.sh / kr260_deploy.sh EXACTLY
# ---------------------------------------------------------
#   password set + sshpass present -> `sshpass -e ssh …` ($SSHPASS keeps it out
#                                     of the local process list)
#   password set, no sshpass       -> key auth for login, password still used
#                                     for on-board sudo
#   no password                    -> key auth + `sudo -n` (needs NOPASSWD)
# On-board privilege is `sudo -S` reading the password as the FIRST line of the
# agent's stdin — sudo consumes exactly that line and the agent inherits the
# rest of the stream, which is what lets one pipe carry both.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""Pluggable board transports. Only ``Ssh*`` touch hardware; both are lazy."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from typing import Dict, Iterable, List, Optional, Sequence

from .log import get_logger
from .safety import TransportError, WedgeDetected

__all__ = [
    "Transport", "MemoryTransport", "SshAgentTransport", "SshOneShotTransport",
    "make_transport", "resolve_password", "ssh_argv",
]

_log = get_logger("transport")

#: Where the agent is staged on the board. Mirrors the `~/td` staging idea of
#: kr260_deploy.sh but namespaced so the two tools never collide.
REMOTE_DIR = os.environ.get("HETSOC_REMOTE_DIR", ".hetsoc")
REMOTE_AGENT = "%s/hetsoc_agent.py" % REMOTE_DIR

SSH_COMMON = (
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=15",
    "-o", "BatchMode=no",
)


def resolve_password(explicit: Optional[str] = None) -> Optional[str]:
    """Board password: explicit > ``HETSOC_PASSWORD`` > ``KR260_PASSWORD``.

    ``KR260_PASSWORD`` is honoured because every existing runbook, Makefile and
    fpgahub action already sets it.
    """
    if explicit:
        return explicit
    for var in ("HETSOC_PASSWORD", "KR260_PASSWORD"):
        value = os.environ.get(var)
        if value:
            return value
    return None


def ssh_argv(host: str, password: Optional[str] = None,
             proxy: Optional[str] = None,
             control_path: Optional[str] = None,
             extra: Sequence[str] = ()) -> List[str]:
    """Build the ssh command prefix, honouring the lab's auth conventions."""
    argv: List[str] = []
    if password and shutil.which("sshpass"):
        # `-e` reads $SSHPASS: keeps the password out of `ps` on this host.
        argv += ["sshpass", "-e"]
    argv += ["ssh"]
    argv += list(SSH_COMMON)
    if proxy:
        argv += ["-o", "ProxyJump=%s" % proxy]
    if control_path:
        argv += ["-o", "ControlMaster=auto",
                 "-o", "ControlPath=%s" % control_path,
                 "-o", "ControlPersist=120"]
    argv += list(extra)
    argv += [host]
    return argv


def _ssh_env(password: Optional[str]) -> Dict[str, str]:
    env = dict(os.environ)
    if password and shutil.which("sshpass"):
        env["SSHPASS"] = password
    return env


# =============================================================================
# Interface
# =============================================================================
class Transport:
    """A 32-bit accessor for one board, addressed in PS PHYSICAL addresses.

    Implementations receive addresses that have ALREADY passed the target's
    guard (``Target.to_host``). They must still refuse out-of-window addresses —
    two independent guards for one unrecoverable hazard.
    """

    name = "transport"

    def read32(self, phys: int) -> int:
        raise NotImplementedError

    def write32(self, phys: int, value: int) -> None:
        raise NotImplementedError

    def read_many(self, phys: int, count: int) -> List[int]:
        return [self.read32(phys + 4 * i) for i in range(count)]

    def ping(self) -> bool:
        """Cheap liveness check of the transport itself (not of the SoC)."""
        return True

    def open(self) -> "Transport":
        return self

    def close(self) -> None:
        pass

    def describe(self) -> str:
        return self.name

    def __enter__(self) -> "Transport":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()


# =============================================================================
# L0 — no board, no network
# =============================================================================
class MemoryTransport(Transport):
    """Dict-backed fake board. The reason L0 tests need no hardware.

    Enforces the same window bound the on-board agent does, and can be told to
    *hang* on chosen addresses so the `guarded()` -> `WedgeDetected` path is
    testable without wedging a real board.
    """

    name = "memory"

    def __init__(self, window_base: int = 0, window_size: int = 1 << 40,
                 initial: Optional[Dict[int, int]] = None,
                 enforce_window: bool = True) -> None:
        self.window_base = window_base
        self.window_size = window_size
        self.enforce_window = enforce_window
        self.mem: Dict[int, int] = dict(initial or {})
        self.reads: List[int] = []
        self.writes: List[tuple] = []
        #: PS phys addresses that block forever when touched (simulated wedge).
        self.hang_addresses: set = set()
        #: PS phys addresses that raise TransportError (simulated SLVERR/agent error).
        self.error_addresses: set = set()
        self._hang = threading.Event()

    # -- guards --------------------------------------------------------- #
    def _check(self, phys: int) -> None:
        if phys & 3:
            raise TransportError("0x%X is not 4-byte aligned" % phys)
        if not self.enforce_window:
            return
        if not (self.window_base <= phys < self.window_base + self.window_size):
            raise TransportError(
                "0x%X is outside the transport window 0x%X..0x%X — the on-board "
                "agent would refuse this too (an undecoded PS access hangs the "
                "AXI bus with no timeout)."
                % (phys, self.window_base,
                   self.window_base + self.window_size - 1))
        if phys in self.hang_addresses:
            self._hang.wait()        # blocks until released — a simulated wedge
        if phys in self.error_addresses:
            raise TransportError("simulated agent error at 0x%X" % phys)

    def release_hangs(self) -> None:
        """Unblock anything waiting in a simulated wedge (test teardown)."""
        self._hang.set()

    # -- ops ------------------------------------------------------------ #
    def read32(self, phys: int) -> int:
        self._check(phys)
        self.reads.append(phys)
        return self.mem.get(phys, 0) & 0xFFFFFFFF

    def write32(self, phys: int, value: int) -> None:
        self._check(phys)
        self.writes.append((phys, value & 0xFFFFFFFF))
        self.mem[phys] = value & 0xFFFFFFFF

    def preload(self, phys: int, values: Iterable[int]) -> None:
        """Seed consecutive words (e.g. a boot-ROM vector table)."""
        for i, value in enumerate(values):
            self.mem[phys + 4 * i] = value & 0xFFFFFFFF

    def describe(self) -> str:
        return "memory[0x%X+0x%X]" % (self.window_base, self.window_size)


# =============================================================================
# SSH — the real board
# =============================================================================
class _SshBase(Transport):
    def __init__(self, host: str, window_base: int, window_size: int,
                 password: Optional[str] = None, proxy: Optional[str] = None,
                 python: str = "python3", timeout_s: float = 30.0) -> None:
        if not host:
            raise TransportError("no board host given (want 'ubuntu@10.22.24.159')")
        self.host = host
        self.window_base = window_base
        self.window_size = window_size
        self.password = resolve_password(password)
        self.proxy = proxy or os.environ.get("HETSOC_PROXY") or os.environ.get("KR260_PROXY")
        self.python = python
        self.timeout_s = timeout_s
        self._log = _log.bind(host=host)

    # -- shared helpers -------------------------------------------------- #
    def _sudo_prefix(self) -> List[str]:
        """`sudo -S -p ''` when a password is available, else `sudo -n`.

        With ``-S`` sudo reads the password as the first line of stdin and the
        command then inherits the remainder of the stream — which is exactly how
        one pipe carries both the credential and the agent protocol.
        """
        if self.password:
            return ["sudo", "-S", "-p", "''"]
        return ["sudo", "-n"]

    def _agent_cmd(self) -> str:
        return " ".join(self._sudo_prefix() + [
            self.python, "-u", REMOTE_AGENT,
            "--window-base", "0x%X" % self.window_base,
            "--window-size", "0x%X" % self.window_size,
        ])

    def _stage_agent(self) -> None:
        """Copy hetsoc/agent.py to the board (idempotent, ~1 round trip)."""
        source = agent_source()
        argv = ssh_argv(self.host, self.password, self.proxy,
                        control_path=getattr(self, "_control_path", None))
        argv += ["mkdir -p %s && cat > %s && chmod +x %s"
                 % (REMOTE_DIR, REMOTE_AGENT, REMOTE_AGENT)]
        try:
            done = subprocess.run(argv, input=source.encode(), env=_ssh_env(self.password),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            raise WedgeDetected(
                "staging the hetsoc agent on %s timed out after %.0fs. If the "
                "board stopped answering ping, the PS AXI bus is wedged — "
                "recover with a JTAG POR (`hetsoc recover`)."
                % (self.host, self.timeout_s))
        except OSError as exc:
            raise TransportError("could not run ssh to %s: %s" % (self.host, exc))
        if done.returncode != 0:
            raise TransportError(
                "staging the hetsoc agent on %s failed (rc=%d):\n%s\n"
                "  Check ssh auth: set HETSOC_PASSWORD/KR260_PASSWORD for "
                "password login (needs sshpass), or stage a key with "
                "`ssh-copy-id %s`."
                % (self.host, done.returncode,
                   done.stderr.decode(errors="replace").strip(), self.host))
        self._log.debug("agent staged", path=REMOTE_AGENT)

    # -- protocol -------------------------------------------------------- #
    @staticmethod
    def _parse(response: str, request: str) -> List[int]:
        response = response.strip()
        if response.startswith("!"):
            raise TransportError("board refused %r: %s"
                                 % (request, response[1:].strip()))
        if response == "ok":
            return []
        if not response.startswith("="):
            raise TransportError("unexpected agent response %r to %r"
                                 % (response, request))
        return [int(tok, 0) for tok in response[1:].split()]

    def read32(self, phys: int) -> int:
        return self.request("r 0x%X" % phys)[0]

    def write32(self, phys: int, value: int) -> None:
        self.request("w 0x%X 0x%X" % (phys, value & 0xFFFFFFFF))

    def read_many(self, phys: int, count: int) -> List[int]:
        if count <= 0:
            return []
        return self.request("m 0x%X 0x%X" % (phys, count))

    def ping(self) -> bool:
        try:
            self.request_raw("p")
            return True
        except (TransportError, WedgeDetected):
            return False

    def request(self, line: str) -> List[int]:
        return self._parse(self.request_raw(line), line)

    def request_raw(self, line: str) -> str:
        raise NotImplementedError


class SshAgentTransport(_SshBase):
    """One persistent SSH connection driving one long-lived on-board agent."""

    name = "ssh-agent"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._tmpdir: Optional[str] = None
        self._control_path: Optional[str] = None
        self._banner = ""

    # -- lifecycle ------------------------------------------------------- #
    def open(self) -> "SshAgentTransport":
        if self._proc is not None and self._proc.poll() is None:
            return self
        self._tmpdir = tempfile.mkdtemp(prefix="hetsoc-ssh-")
        # Short path: ControlPath has a ~104-byte sockaddr limit.
        self._control_path = os.path.join(self._tmpdir, "cm")
        self._stage_agent()

        argv = ssh_argv(self.host, self.password, self.proxy,
                        control_path=self._control_path)
        argv += [self._agent_cmd()]
        try:
            self._proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=_ssh_env(self.password),
                bufsize=1, universal_newlines=True)
        except OSError as exc:
            raise TransportError("could not start ssh to %s: %s" % (self.host, exc))

        if self.password:
            # sudo -S eats exactly this line; the agent inherits the rest.
            self._write("%s\n" % self.password)
        banner = self._read_line(self.timeout_s, what="agent startup")
        if not banner.startswith("ready"):
            self.close()
            raise TransportError(
                "hetsoc agent on %s did not come up (said %r).\n"
                "  Usual causes: sudo needs a password (set HETSOC_PASSWORD / "
                "KR260_PASSWORD) or NOPASSWD; python3 missing on the board."
                % (self.host, banner.strip()))
        self._banner = banner.strip()
        self._log.info("agent up", banner=self._banner,
                       window=self.window_base, size=self.window_size)
        return self

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.write("q\n")
                    proc.stdin.flush()
                proc.wait(timeout=3)
            except Exception:                            # noqa: BLE001
                try:
                    proc.kill()
                except Exception:                        # noqa: BLE001
                    pass
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    # -- io -------------------------------------------------------------- #
    def _write(self, text: str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise TransportError("transport to %s is not open" % self.host)
        try:
            proc.stdin.write(text)
            proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise TransportError(
                "the on-board agent on %s died (%s). stderr:\n%s"
                % (self.host, exc, self._drain_stderr()))

    def _read_line(self, timeout_s: float, what: str = "response") -> str:
        """Read one line with a hard bound — a wedged bus never answers."""
        import select

        proc = self._proc
        if proc is None or proc.stdout is None:
            raise TransportError("transport to %s is not open" % self.host)
        ready, _, _ = select.select([proc.stdout], [], [], timeout_s)
        if not ready:
            raise WedgeDetected(
                "no %s from %s within %.1fs — treating the board as WEDGED.\n"
                "  The on-board agent stops answering when a PS access hangs the "
                "ZynqMP AXI bus, which has no timeout. Do NOT retry; recover with "
                "a JTAG POR (`hetsoc recover`), then re-deploy before bringing "
                "the link up again." % (what, self.host, timeout_s))
        line = proc.stdout.readline()
        if line == "":
            raise TransportError(
                "the on-board agent on %s closed its output. stderr:\n%s"
                % (self.host, self._drain_stderr()))
        return line

    def _drain_stderr(self) -> str:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return "(none)"
        try:
            import select
            ready, _, _ = select.select([proc.stderr], [], [], 0.2)
            return proc.stderr.readline().strip() if ready else "(none)"
        except Exception:                                # noqa: BLE001
            return "(none)"

    def request_raw(self, line: str) -> str:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self.open()
            self._write(line.strip() + "\n")
            return self._read_line(self.timeout_s, what="response to %r" % line)

    def describe(self) -> str:
        return "ssh-agent://%s [%s]" % (self.host, self._banner or "not open")


class SshOneShotTransport(_SshBase):
    """One ssh invocation per request. The no-moving-parts fallback.

    Shape-identical to what kr260_eth_run.sh does today (`ssh … "cd td && echo
    <pw> | sudo -S python3 …"`), so if the persistent path ever misbehaves the
    bench still has the exact flow that is proven on silicon.
    """

    name = "ssh-oneshot"

    def request_raw(self, line: str) -> str:
        return self.request_batch([line])[0]

    def request_batch(self, lines: Sequence[str]) -> List[str]:
        payload = ""
        if self.password:
            payload += "%s\n" % self.password
        payload += "".join("%s\n" % ln.strip() for ln in lines) + "q\n"
        argv = ssh_argv(self.host, self.password, self.proxy)
        argv += [self._agent_cmd()]
        try:
            done = subprocess.run(argv, input=payload.encode(),
                                  env=_ssh_env(self.password),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            raise WedgeDetected(
                "ssh request to %s timed out after %.0fs — treating the board as "
                "WEDGED (a hung PS AXI access has no timeout). Recover with a "
                "JTAG POR (`hetsoc recover`)." % (self.host, self.timeout_s))
        except OSError as exc:
            raise TransportError("could not run ssh to %s: %s" % (self.host, exc))
        out = [ln for ln in done.stdout.decode(errors="replace").splitlines() if ln.strip()]
        if done.returncode != 0 and not out:
            raise TransportError(
                "ssh to %s failed (rc=%d):\n%s"
                % (self.host, done.returncode,
                   done.stderr.decode(errors="replace").strip()))
        # Drop the `ready …` banner and the trailing `bye`.
        body = [ln for ln in out if not ln.startswith("ready") and ln != "bye"]
        if len(body) < len(lines):
            raise TransportError(
                "agent on %s returned %d responses for %d requests:\n%s"
                % (self.host, len(body), len(lines), "\n".join(out)))
        return body[:len(lines)]

    def open(self) -> "SshOneShotTransport":
        self._stage_agent()
        return self

    def describe(self) -> str:
        return "ssh-oneshot://%s" % self.host


# =============================================================================
# Helpers
# =============================================================================
def agent_source() -> str:
    """Source text of ``hetsoc/agent.py``, for staging onto the board."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.py")
    with open(path, "r") as handle:
        return handle.read()


def make_transport(kind: str, host: str, window_base: int, window_size: int,
                   **kwargs) -> Transport:
    """Factory. ``kind`` in {"ssh", "ssh-agent", "ssh-oneshot", "memory"}.

    ``HETSOC_TRANSPORT`` overrides the default, so an operator can drop to the
    one-shot path without editing code.
    """
    kind = (kind or os.environ.get("HETSOC_TRANSPORT") or "ssh-agent").lower()
    if kind in ("memory", "mock", "none"):
        return MemoryTransport(window_base, window_size)
    if kind in ("ssh", "ssh-agent", "agent"):
        return SshAgentTransport(host, window_base, window_size, **kwargs)
    if kind in ("ssh-oneshot", "oneshot"):
        return SshOneShotTransport(host, window_base, window_size, **kwargs)
    raise TransportError(
        "unknown transport %r (want ssh-agent | ssh-oneshot | memory)" % kind)
