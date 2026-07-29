# =============================================================================
# L0 — fpgahub command construction. NOTHING is executed here.
#
# A JTAG POR is the ONLY recovery from a wedged PS AXI bus, so the two
# documented quirks are asserted as properties of the commands this module
# builds:
#   * per-board endpoints run on the daemon host (they 404 from other clients);
#   * the reset is the SINGLE-MEMBER curl form (the group `board reset` breaks
#     on the board's `_pl` topology entry).
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
import json

import pytest

from hetsoc import fpgahub
from hetsoc.fpgahub import FpgahubError


class _Recorder:
    """Captures every command instead of running it."""

    def __init__(self, fail_on=None, output=""):
        self.calls = []
        self.fail_on = fail_on or ()
        self.output = output

    def __call__(self, command, timeout_s=None, shell=False, check=True):
        printable = command if isinstance(command, str) else " ".join(command)
        self.calls.append(printable)
        for needle in self.fail_on:
            if needle in printable:
                raise FpgahubError("simulated failure: %s" % printable)
        return self.output


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(fpgahub, "run_local", rec)
    return rec


# =============================================================================
class TestReset:
    def test_the_default_is_the_single_member_curl_workaround(self, recorder):
        fpgahub.reset("kr260_01")
        command = recorder.calls[0]
        assert "curl" in command
        assert "--unix-socket /run/fpgahub/fpgahub.sock" in command
        assert "/api/v1/targets/kr260_01/reset" in command
        assert "-X POST" in command

    def test_the_payload_is_the_documented_one(self, recorder):
        fpgahub.reset("kr260_01")
        command = recorder.calls[0]
        assert json.dumps({"method": "default", "confirm": True}) in command

    def test_it_runs_on_the_daemon_host_not_here(self, recorder):
        # Per-board endpoints 404 from other client hosts.
        fpgahub.reset("kr260_01")
        assert recorder.calls[0].startswith("ssh")
        assert fpgahub.HUB_HOST in recorder.calls[0]

    def test_it_does_NOT_use_the_group_board_reset_first(self, recorder):
        # The group reset breaks on the `_pl` topology entry.
        fpgahub.reset("kr260_01")
        assert "board reset" not in recorder.calls[0]

    def test_it_falls_back_to_the_cli_when_curl_fails(self, monkeypatch):
        rec = _Recorder(fail_on=("curl",))
        monkeypatch.setattr(fpgahub, "run_local", rec)
        fpgahub.reset("kr260_01")
        assert len(rec.calls) == 2
        assert "curl" in rec.calls[0]
        assert "fpgahub board reset kr260_01 --yes" in rec.calls[1]

    def test_both_failing_raises_with_the_manual_command(self, monkeypatch):
        rec = _Recorder(fail_on=("curl", "board reset"))
        monkeypatch.setattr(fpgahub, "run_local", rec)
        with pytest.raises(FpgahubError) as info:
            fpgahub.reset("kr260_01")
        message = str(info.value)
        assert "only recovery" in message
        assert "ssh %s" % fpgahub.HUB_HOST in message
        assert "curl" in message

    def test_method_curl_skips_the_cli(self, monkeypatch):
        rec = _Recorder(fail_on=("curl",))
        monkeypatch.setattr(fpgahub, "run_local", rec)
        with pytest.raises(FpgahubError):
            fpgahub.reset("kr260_01", method="curl")
        assert len(rec.calls) == 1

    def test_method_cli_skips_the_curl(self, recorder):
        fpgahub.reset("kr260_01", method="cli")
        assert "curl" not in recorder.calls[0]
        assert "board reset" in recorder.calls[0]

    def test_an_unknown_method_is_refused(self):
        with pytest.raises(ValueError):
            fpgahub.reset("kr260_01", method="magic")

    def test_the_board_name_is_shell_quoted(self, recorder):
        # The command is composed into a remote shell string, so a board name
        # from a config file must not be able to break out of it.
        fpgahub.reset("kr260_01; rm -rf /")
        assert "'kr260_01; rm -rf /'" in recorder.calls[0]


# =============================================================================
class TestLease:
    def test_collection_endpoints_run_locally(self, recorder):
        fpgahub.acquire("kr260_01")
        assert not recorder.calls[0].startswith("ssh"), \
            "lease/status are collection endpoints and work from any host"
        assert "fpgahub lease acquire kr260_01" in recorder.calls[0]

    def test_the_context_manager_acquires_and_releases_both(self, recorder):
        with fpgahub.lease("kr260_01", "kr260_02") as held:
            assert held == ["kr260_01", "kr260_02"]
        assert "lease acquire kr260_01" in recorder.calls[0]
        assert "lease acquire kr260_02" in recorder.calls[1]
        assert "lease release kr260_02" in recorder.calls[2]
        assert "lease release kr260_01" in recorder.calls[3]

    def test_leases_are_released_even_when_the_body_raises(self, recorder):
        with pytest.raises(RuntimeError):
            with fpgahub.lease("kr260_01"):
                raise RuntimeError("test failed mid-run")
        assert any("lease release kr260_01" in c for c in recorder.calls)

    def test_a_failed_acquire_releases_what_was_already_held(self, monkeypatch):
        rec = _Recorder(fail_on=("acquire kr260_02",))
        monkeypatch.setattr(fpgahub, "run_local", rec)
        with pytest.raises(FpgahubError):
            with fpgahub.lease("kr260_01", "kr260_02"):
                pass
        assert any("lease release kr260_01" in c for c in rec.calls)

    def test_release_failures_do_not_mask_a_test_failure(self, monkeypatch):
        rec = _Recorder(fail_on=("release",))
        monkeypatch.setattr(fpgahub, "run_local", rec)
        with fpgahub.lease("kr260_01"):
            pass                              # must not raise


# =============================================================================
class TestPerBoardEndpoints:
    def test_actions_run_on_the_daemon_host(self, recorder):
        fpgahub.run_action("kr260_01", "deploy_kr260_eth_chiplet_pair")
        assert recorder.calls[0].startswith("ssh")
        assert fpgahub.HUB_HOST in recorder.calls[0]
        assert "fpgahub actions run kr260_01 deploy_kr260_eth_chiplet_pair" \
            in recorder.calls[0]

    def test_board_show_runs_on_the_daemon_host(self, recorder):
        fpgahub.board_show("kr260_01")
        assert recorder.calls[0].startswith("ssh")

    def test_local_mode_skips_the_ssh_hop(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(fpgahub, "run_local", rec)
        monkeypatch.setenv("HETSOC_FPGAHUB_LOCAL", "1")
        fpgahub.board_show("kr260_01")
        assert not rec.calls[0].startswith("ssh")


# =============================================================================
class TestStatus:
    def test_status_parses_json(self, monkeypatch):
        payload = {"boards": [{"name": "kr260_01", "lease": None},
                              {"name": "kr260_02", "lease": {"owner": "x"}}]}
        monkeypatch.setattr(fpgahub, "run_local",
                            _Recorder(output=json.dumps(payload)))
        assert fpgahub.status() == payload

    def test_free_boards_reads_the_lease_field(self, monkeypatch):
        payload = {"boards": [{"name": "kr260_01", "lease": None},
                              {"name": "kr260_02", "lease": {"owner": "x"}}]}
        monkeypatch.setattr(fpgahub, "run_local",
                            _Recorder(output=json.dumps(payload)))
        assert fpgahub.free_boards() == {"kr260_01": True, "kr260_02": False}

    def test_status_failure_is_actionable(self, monkeypatch):
        monkeypatch.setattr(fpgahub, "run_local", _Recorder(fail_on=("fpgahub",)))
        with pytest.raises(FpgahubError) as info:
            fpgahub.status()
        assert "fpgahubd" in str(info.value)
