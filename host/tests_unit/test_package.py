# =============================================================================
# L0 — the import contract, the transport plumbing and the CLI surface.
#
# THE IMPORT CONTRACT IS LOAD-BEARING: `import hetsoc` must work on a machine
# with no board, no network, no /dev/mem and no third-party packages. That is
# what lets this tier run in any CI container, which is what makes the address
# guards testable at all.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
import subprocess
import sys

import pytest

import hetsoc
from hetsoc import agent, cli, transport
from hetsoc.safety import TransportError


# =============================================================================
class TestImportContract:
    def test_importing_hetsoc_pulls_in_no_hardware_modules(self):
        code = (
            "import sys, hetsoc;"
            "bad = [m for m in ('pynq', 'mmap') if m in sys.modules];"
            "print(bad)"
        )
        out = subprocess.check_output([sys.executable, "-c", code]).decode()
        assert out.strip() == "[]", "hetsoc must not import pynq/mmap eagerly"

    def test_importing_hetsoc_pulls_in_no_subprocess_module(self):
        # subprocess arriving at import time means a hardware path is eager.
        code = ("import sys, hetsoc;"
                "print('hetsoc.transport' in sys.modules,"
                " 'hetsoc.fpgahub' in sys.modules)")
        out = subprocess.check_output([sys.executable, "-c", code]).decode()
        assert out.strip() == "False False"

    def test_the_contract_surface_is_exported(self):
        for name in ("Target", "TARGETS", "get_target", "AddressGuardError",
                     "LinkDownError", "WedgeDetected", "require_link_up",
                     "guarded", "cam_rule", "decode_lane_status",
                     "FCSM_LINK_IDLE", "Board", "ChipletPair"):
            assert hasattr(hetsoc, name), "hetsoc.%s is in the API contract" % name

    def test_lazy_attributes_resolve(self):
        assert hetsoc.Board.__name__ == "Board"
        assert hetsoc.ChipletPair.__name__ == "ChipletPair"
        assert callable(hetsoc.fpgahub.reset)

    def test_an_unknown_attribute_still_raises_attributeerror(self):
        with pytest.raises(AttributeError):
            hetsoc.definitely_not_a_thing

    def test_version_is_present(self):
        assert hetsoc.__version__


# =============================================================================
class TestTransportFactory:
    def test_memory_transport_needs_no_network(self):
        got = transport.make_transport("memory", "", 0x4_0000_0000, 0x1000)
        assert isinstance(got, transport.MemoryTransport)
        got.write32(0x4_0000_0000, 0xC0FFEE01)
        assert got.read32(0x4_0000_0000) == 0xC0FFEE01

    def test_ssh_transports_are_constructed_but_not_connected(self):
        got = transport.make_transport("ssh-agent", "ubuntu@10.22.24.159",
                                       0x4_0000_0000, 0x1_0000_0000)
        assert isinstance(got, transport.SshAgentTransport)
        assert "10.22.24.159" in got.describe()

    def test_an_unknown_transport_is_refused(self):
        with pytest.raises(TransportError):
            transport.make_transport("carrier-pigeon", "h", 0, 1)

    def test_a_transport_without_a_host_is_refused(self):
        with pytest.raises(TransportError):
            transport.SshAgentTransport("", 0, 1)

    def test_env_overrides_the_default_kind(self, monkeypatch):
        monkeypatch.setenv("HETSOC_TRANSPORT", "memory")
        assert isinstance(transport.make_transport(None, "h", 0, 0x1000),
                          transport.MemoryTransport)


class TestSshAuthShape:
    """Mirrors kr260_eth_run.sh / kr260_deploy.sh exactly."""

    def test_password_uses_sshpass_dash_e_when_available(self, monkeypatch):
        monkeypatch.setattr(transport.shutil, "which", lambda _n: "/usr/bin/sshpass")
        argv = transport.ssh_argv("ubuntu@1.2.3.4", password="secret")
        assert argv[:2] == ["sshpass", "-e"], \
            "-e reads $SSHPASS, keeping the password out of `ps`"
        assert "secret" not in " ".join(argv)

    def test_no_password_means_key_auth(self, monkeypatch):
        monkeypatch.setattr(transport.shutil, "which", lambda _n: None)
        assert transport.ssh_argv("ubuntu@1.2.3.4")[0] == "ssh"

    def test_password_without_sshpass_still_uses_key_auth_for_login(self,
                                                                    monkeypatch):
        monkeypatch.setattr(transport.shutil, "which", lambda _n: None)
        assert transport.ssh_argv("ubuntu@1.2.3.4", password="secret")[0] == "ssh"

    def test_sudo_is_dash_S_with_a_password_and_dash_n_without(self, monkeypatch):
        monkeypatch.delenv("HETSOC_PASSWORD", raising=False)
        monkeypatch.delenv("KR260_PASSWORD", raising=False)
        assert transport.SshAgentTransport(
            "h", 0, 1)._sudo_prefix() == ["sudo", "-n"]
        assert transport.SshAgentTransport(
            "h", 0, 1, password="pw")._sudo_prefix()[:2] == ["sudo", "-S"]

    def test_kr260_password_env_is_honoured(self, monkeypatch):
        monkeypatch.delenv("HETSOC_PASSWORD", raising=False)
        monkeypatch.setenv("KR260_PASSWORD", "from-runbook")
        assert transport.resolve_password() == "from-runbook"

    def test_hetsoc_password_wins_over_kr260_password(self, monkeypatch):
        monkeypatch.setenv("HETSOC_PASSWORD", "new")
        monkeypatch.setenv("KR260_PASSWORD", "old")
        assert transport.resolve_password() == "new"

    def test_the_agent_command_carries_the_window_bound(self):
        command = transport.SshAgentTransport(
            "h", 0x4_0000_0000, 0x1_0000_0000)._agent_cmd()
        assert "--window-base 0x400000000" in command
        assert "--window-size 0x100000000" in command


# =============================================================================
class TestOnBoardAgent:
    """The agent re-checks the window: two independent guards, one hazard."""

    def test_it_refuses_an_address_below_the_window(self):
        instance = agent.Agent(0x4_0000_0000, 0x1_0000_0000)
        with pytest.raises(agent.WindowError):
            instance._check(0x8403_2108)

    def test_it_refuses_an_address_past_the_window(self):
        instance = agent.Agent(0x4_0000_0000, 0x1000)
        with pytest.raises(agent.WindowError):
            instance._check(0x4_0000_1000)

    def test_a_burst_that_walks_off_the_end_is_refused(self):
        instance = agent.Agent(0x4_0000_0000, 0x1000)
        instance._check(0x4_0000_0FFC, nwords=1)
        with pytest.raises(agent.WindowError):
            instance._check(0x4_0000_0FFC, nwords=2)

    def test_unaligned_is_refused(self):
        instance = agent.Agent(0x4_0000_0000, 0x1000)
        with pytest.raises(agent.WindowError):
            instance._check(0x4_0000_0002)

    def test_a_zero_sized_window_is_refused(self):
        with pytest.raises(ValueError):
            agent.Agent(0x4_0000_0000, 0)

    def test_the_agent_source_can_be_staged(self):
        source = transport.agent_source()
        assert "hetsoc" in source and "def serve" in source
        compile(source, "hetsoc_agent.py", "exec")     # must be valid Python

    def test_the_agent_imports_nothing_from_hetsoc(self):
        # It is copied to a plain-Ubuntu board and run by the stock python3.
        source = transport.agent_source()
        assert "from hetsoc" not in source
        assert "import hetsoc" not in source


class TestAgentProtocol:
    """Drive the agent's line protocol against a fake accessor."""

    class _FakeAgent(agent.Agent):
        def __init__(self):
            agent.Agent.__init__(self, 0x4_0000_0000, 0x1_0000_0000)
            self.store = {}

        def _page(self, phys):
            raise AssertionError("must not mmap in a unit test")

        def read(self, phys):
            self._check(phys)
            return self.store.get(phys, 0)

        def write(self, phys, value):
            self._check(phys)
            self.store[phys] = value & 0xFFFFFFFF

    def _serve(self, requests):
        import io

        instance = self._FakeAgent()
        out = io.StringIO()
        agent.serve(instance, stdin=io.StringIO("".join(
            "%s\n" % r for r in requests)), stdout=out)
        return out.getvalue().splitlines()

    def test_ready_banner_then_one_response_per_request(self):
        lines = self._serve(["p", "w 0x400000000 0xC0FFEE01", "r 0x400000000"])
        assert lines[0].startswith("ready")
        assert lines[1].startswith("pong")
        assert lines[2] == "ok"
        assert lines[3] == "= 0xC0FFEE01"

    def test_out_of_window_gets_an_error_line_not_an_access(self):
        lines = self._serve(["r 0x84032108"])
        assert lines[1].startswith("!")
        assert "outside" in lines[1]

    def test_a_multiword_read_returns_one_line(self):
        lines = self._serve(["w 0x400000000 0x1", "w 0x400000004 0x2",
                             "m 0x400000000 0x2"])
        assert lines[-1] == "= 0x00000001 0x00000002"

    def test_a_bad_request_does_not_kill_the_agent(self):
        lines = self._serve(["nonsense", "p"])
        assert lines[1].startswith("! unknown op")
        assert lines[2].startswith("pong")

    def test_quit_is_acknowledged(self):
        assert self._serve(["q"])[-1] == "bye"


# =============================================================================
class TestCli:
    def test_help_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as info:
            cli.main(["--help"])
        assert info.value.code == 0

    def test_no_command_prints_help_and_returns_usage(self, capsys):
        assert cli.main([]) == cli.EXIT_USAGE

    def test_the_contract_commands_exist(self):
        parser = cli.build_parser()
        actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
        commands = set()
        for action in actions:
            commands.update(action.choices or {})
        for required in ("status", "bringup", "verify", "health", "recover"):
            assert required in commands

    def test_targets_needs_no_config_and_no_boards(self, capsys):
        assert cli.main(["targets"]) == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "kr260-eth-chiplet" in out
        assert "PROVISIONAL" in out, "the compute target must be flagged"

    def test_targets_shows_the_window_and_the_peer_gate(self, capsys):
        cli.main(["targets", "kr260-eth-chiplet"])
        out = capsys.readouterr().out
        assert "0x400000000" in out.replace("_", "")
        assert "FCSM=4" in out

    def test_regs_needs_no_board(self, capsys):
        assert cli.main(["regs"]) == cli.EXIT_OK
        assert "SWI_LANE_STATUS" in capsys.readouterr().out

    def test_a_missing_config_is_a_usage_error_not_a_crash(self, monkeypatch,
                                                           tmp_path, capsys):
        monkeypatch.delenv("HETSOC_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert cli.main(["verify"]) == cli.EXIT_USAGE
        assert "hetsoc.toml.example" in capsys.readouterr().err
