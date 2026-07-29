# =============================================================================
# L0 — the guards themselves.
#
# `guarded()` is the difference between "the board wedged" and "the board wedged
# AND the test runner hung with it". A wedged PS AXI bus never returns, so the
# hang is tested for real here: a MemoryTransport address that blocks forever,
# and an assertion that the call comes back as WedgeDetected within the budget.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
import time

import pytest

from hetsoc import regs
from hetsoc.safety import (AddressGuardError, HetsocError, LinkDownError,
                           ProvisionalTargetError, WedgeDetected, guarded,
                           require_link_up, run_guarded)


class _FakeBoard:
    def __init__(self, raw):
        self.name = "fake"
        self._raw = raw

    def lane_status(self):
        return regs.decode_lane_status(self._raw)


# =============================================================================
class TestExceptionHierarchy:
    def test_everything_derives_from_hetsocerror(self):
        for cls in (AddressGuardError, LinkDownError, WedgeDetected,
                    ProvisionalTargetError):
            assert issubclass(cls, HetsocError)

    def test_provisional_is_an_address_guard_error(self):
        # so `except AddressGuardError` still stops an unresolved target.
        assert issubclass(ProvisionalTargetError, AddressGuardError)


# =============================================================================
class TestRequireLinkUp:
    def test_passes_on_the_silicon_up_value(self):
        require_link_up(_FakeBoard(0x05890000))       # FCSM=4, cal_done=1

    @pytest.mark.parametrize("raw", [
        0x00000000,                # nothing up
        (3 << 17) | (1 << 16),     # calibrated but not LINK_IDLE
        (4 << 17),                 # FCSM=4 but calibration never completed
    ])
    def test_raises_link_down_on_anything_else(self, raw):
        with pytest.raises(LinkDownError) as info:
            require_link_up(_FakeBoard(raw))
        message = str(info.value)
        assert "hanging the PS AXI bus" in message
        assert "BOTH boards" in message

    def test_error_names_the_board_and_the_raw_word(self):
        with pytest.raises(LinkDownError) as info:
            require_link_up(_FakeBoard(0x00020000))
        assert "fake" in str(info.value)
        assert "0x00020000" in str(info.value)


# =============================================================================
class TestGuarded:
    def test_returns_the_value_when_the_call_completes(self):
        @guarded(2.0)
        def add(a, b):
            return a + b

        assert add(2, 3) == 5

    def test_a_hang_becomes_wedgedetected_within_the_budget(self):
        import threading

        never = threading.Event()

        @guarded(0.25)
        def blocks():
            never.wait()          # exactly what a hung PS AXI read looks like

        started = time.time()
        with pytest.raises(WedgeDetected) as info:
            blocks()
        elapsed = time.time() - started
        assert elapsed < 3.0, "guarded() must not wait past its budget"
        assert "WEDGED" in str(info.value)
        assert "JTAG POR" in str(info.value)
        never.set()

    def test_the_original_exception_is_re_raised_unchanged(self):
        @guarded(2.0)
        def explodes():
            raise AddressGuardError("out of window")

        with pytest.raises(AddressGuardError):
            explodes()

    def test_metadata_is_preserved_and_the_budget_is_introspectable(self):
        @guarded(1.5)
        def documented():
            """docstring."""
            return 1

        assert documented.__name__ == "documented"
        assert documented.__doc__ == "docstring."
        assert documented.__hetsoc_timeout__ == 1.5

    def test_a_non_positive_timeout_is_refused(self):
        for bad in (0, -1):
            with pytest.raises(ValueError):
                guarded(bad)

    def test_run_guarded_takes_args_and_kwargs(self):
        assert run_guarded(lambda a, b=0: a + b, 2.0, 1, b=2) == 3

    def test_the_worker_thread_is_a_daemon_so_the_interpreter_can_exit(self):
        # A ThreadPoolExecutor would be joined at exit and hang the process on a
        # wedged board; a daemon thread does not.
        import threading

        never = threading.Event()
        seen = {}

        def blocks():
            seen["daemon"] = threading.current_thread().daemon
            never.wait()

        with pytest.raises(WedgeDetected):
            run_guarded(blocks, 0.25)
        never.set()
        assert seen.get("daemon") is True
