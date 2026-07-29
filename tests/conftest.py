"""pytest fixtures, markers and safety gating for the heterogeneous chiplet suite.

Owned by the **Tests** area (`tests/**`, docs/REPO_LAYOUT.md). Consumes
`host/hetsoc/` through `host/API_CONTRACT.md` and never writes to it.

Four things this file exists to enforce:

1. **The wedge-prone levels are opt-in.** L4 (cross-die data plane) and L5 (soak)
   intermittently hang the ZynqMP AXI bus on current silicon: the shipped build
   carries the *upstream, recovery-stripped* FCSM on the five AXI data-plane FC
   nodes, so a single bit error has no recovery path and the far side's response
   beat never returns. Anything marked `data_plane` or `soak` is **deselected**
   unless `--data-plane` is given.
2. **A wedge aborts the session; it does not cascade.** After any hardware-test
   failure the guard probes every board's boot ROM under a hard timeout. If one
   has stopped answering, the run stops with a single actionable report instead
   of forty more timeouts — optionally issuing a JTAG POR first (`--auto-por`).
3. **The pair is a parameter, not a constant.** Only the *homogeneous* eth pair
   has ever run on silicon; the compute chiplet has no KR260 bitstream, so its
   target descriptor is provisional and refuses to produce a host address at all.
   Pair-level tests are parameterised over `--pair` and **skip with a reason**
   when a pair is not runnable.
4. **The link is never brought up on a live link.** `linked_pair` only *verifies*
   unless `--deploy` was given: re-running the bring-up (LL_SWRESET) on an
   already-live link desyncs it and hangs the sender's peer writes.

Copyright (C) 2026, SoC Labs (www.soclabs.org)
"""
from __future__ import annotations

import pathlib
import sys

import pytest

# `hetsoc` lives in host/ (owned by the Framework area; consumed, never written).
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_HOST_DIR = _REPO_ROOT / "host"
if str(_HOST_DIR) not in sys.path:
    sys.path.insert(0, str(_HOST_DIR))

from hetsoc import config as hetsoc_config    # noqa: E402
from hetsoc import fpgahub as hetsoc_fpgahub  # noqa: E402
from hetsoc import regs                       # noqa: E402
from hetsoc import safety                     # noqa: E402

import _helpers as H                          # noqa: E402


# ===========================================================================
# CLI options
# ===========================================================================

def pytest_addoption(parser):
    group = parser.getgroup("hetsoc", "heterogeneous chiplet bench")
    group.addoption(
        "--boards", action="store", default=None, metavar="A,B",
        help="comma-separated board names from the hetsoc config that are "
             "actually available this session (default: every board in the "
             "config). A pair whose boards are not all available is SKIPPED.")
    group.addoption(
        "--pair", action="append", default=[], metavar="NAME",
        help="pair name from the hetsoc config ([pair.<NAME>]). Repeatable — "
             "every pair-level test is parameterised over the pairs given. "
             "Default: 'default'.")
    group.addoption(
        "--data-plane", action="store_true", default=False,
        help="ATTENDED ONLY. Run the cross-die data-plane (L4) and soak (L5) "
             "tests. They push transactions over the D2D link, which "
             "intermittently HANGS the PS AXI bus on current silicon and needs "
             "a JTAG POR to recover. Deselected by default.")
    group.addoption(
        "--soak-iters", action="store", type=int, default=1000, metavar="N",
        help="beats per soak test (default 1000). Only meaningful with "
             "--data-plane.")
    group.addoption(
        "--deploy", action="store_true", default=False,
        help="reflash both dies and run a full concurrent link bring-up before "
             "the session. WITHOUT this the suite only *verifies* an existing "
             "link and never re-runs bring-up — LL_SWRESET on a live link "
             "desyncs it and hangs the sender (wedged die_a 2026-07-29).")
    group.addoption(
        "--allow-peer-read", action="store_true", default=False,
        help="with --data-plane, also run the peer READ round-trip: the most "
             "wedge-prone access on current silicon (the rd_pipe_r "
             "read-completion guard is absent from the shipped tidelink_top). "
             "Off by default — the write path is already proven wedge-safely by "
             "a LOCAL read on the receiver.")
    group.addoption(
        "--auto-por", action="store_true", default=False,
        help="when the wedge guard finds a wedged board, issue a JTAG POR via "
             "fpgahub and re-probe before giving up. Off by default so a wedge "
             "is preserved for diagnosis.")


MARKERS = {
    "hardware": "needs at least one real KR260 (skipped when none is configured)",
    "single_board": "runs against one die at a time; no cross-die traffic",
    "pair": "needs both dies of a configured pair",
    "data_plane": "pushes transactions ACROSS the D2D link - WEDGE RISK, "
                  "deselected unless --data-plane",
    "soak": "sustained cross-die load - WEDGE RISK, deselected unless "
            "--data-plane",
    "peer_read": "reads back over the link (most wedge-prone); needs "
                 "--allow-peer-read",
    "slow": "minutes, not seconds (deploy / POR / soak)",
    "nongating": "diagnostic - a failure is a known gap, not a silicon regression",
    "l0": "L0 - pure host logic, no hardware, always runs in CI",
    "l1": "L1 - one board, read-only probes",
    "l2": "L2 - one board, config-plane writes",
    "l3": "L3 - two boards, link bring-up + control plane",
    "l4": "L4 - two boards, cross-die data plane (HIGH wedge risk)",
    "l5": "L5 - two boards, soak / characterisation (HIGH wedge risk)",
    "l6": "L6 - blocked on firmware or a new port; skips with the blocker named",
}


def pytest_configure(config):
    for name, description in MARKERS.items():
        config.addinivalue_line("markers", "%s: %s" % (name, description))
    config._hetsoc_state = _SessionState()


# ===========================================================================
# Default deselection of the wedge-prone levels
# ===========================================================================

def pytest_collection_modifyitems(config, items):
    if config.getoption("data_plane"):
        return
    keep, drop = [], []
    for item in items:
        risky = (item.get_closest_marker("data_plane")
                 or item.get_closest_marker("soak"))
        (drop if risky else keep).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
        config._hetsoc_deselected = len(drop)


def pytest_report_collectionfinish(config, items):
    lines = []
    dropped = getattr(config, "_hetsoc_deselected", 0)
    if dropped:
        lines.append(
            "hetsoc: %d cross-die data-plane/soak test(s) DESELECTED — they "
            "intermittently wedge the PS AXI bus on current silicon "
            "(docs/SAFETY.md). Pass --data-plane to run them ATTENDED." % dropped)
    if config.getoption("data_plane"):
        lines.append(
            "hetsoc: *** --data-plane ENABLED: cross-die transfers may WEDGE a "
            "board (JTAG-POR-only recovery). Do not leave this unattended. ***")
    return lines


# ===========================================================================
# Wedge guard
# ===========================================================================

class _SessionState:
    """Session-wide bench state: which boards exist, and whether one is wedged."""

    def __init__(self):
        self.boards = {}          # name -> Board, registered as they are built
        self.wedged = False
        self.report = ""

    def register(self, board):
        self.boards[board.name] = board

    def probe_all(self):
        """Boot-ROM aliveness on every known board, each under a hard timeout.
        Returns {name: True | "<error>"}. Never raises."""
        out = {}
        for name, board in self.boards.items():
            try:
                out[name] = bool(H.call_guarded(15.0, board.alive))
            except safety.WedgeDetected as exc:
                out[name] = "WedgeDetected: %s" % exc
            except Exception as exc:                       # noqa: BLE001
                out[name] = "%s: %s" % (type(exc).__name__, exc)
        return out

    def snapshot_all(self):
        """Best-effort decoded health for every board. Never raises."""
        out = {}
        for name, board in self.boards.items():
            try:
                out[name] = H.fmt_health(H.call_guarded(15.0, board.health))
            except Exception as exc:                       # noqa: BLE001
                out[name] = "<unreadable: %s: %s>" % (type(exc).__name__, exc)
        return out


def _state(config) -> _SessionState:
    return config._hetsoc_state


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, "_hetsoc_rep_" + report.when, report)


def pytest_runtest_setup(item):
    state = _state(item.config)
    if state.wedged and item.get_closest_marker("hardware"):
        pytest.exit(state.report, returncode=2)


@pytest.fixture(autouse=True)
def _wedge_guard(request):
    """After ANY failing hardware test, prove the bench is still alive.

    An unrecoverable AXI FC-node stall hangs the ZynqMP bus with no timeout:
    every later test would time out too, and the real first failure would be
    buried under them. So on failure we probe each board's boot ROM under a hard
    timeout and, if a board has stopped answering, abort the whole session with
    one report that says what to do about it.
    """
    yield
    if not request.node.get_closest_marker("hardware"):
        return
    state = _state(request.config)
    if state.wedged or not state.boards:
        return

    call_rep = getattr(request.node, "_hetsoc_rep_call", None)
    setup_rep = getattr(request.node, "_hetsoc_rep_setup", None)
    failed = ((call_rep is not None and call_rep.failed)
              or (setup_rep is not None and setup_rep.failed))
    if not failed:
        return

    probes = state.probe_all()
    dead = [name for name, value in probes.items() if value is not True]
    if not dead:
        return          # a real test failure on a healthy bench — let it stand

    recovered = False
    if request.config.getoption("auto_por"):
        for name in dead:
            try:
                state.boards[name].por()
            except Exception as exc:                        # noqa: BLE001
                print("hetsoc: POR of %s failed: %s: %s"
                      % (name, type(exc).__name__, exc))
        probes = state.probe_all()
        dead = [name for name, value in probes.items() if value is not True]
        recovered = not dead

    state.wedged = True
    if recovered:
        state.report = (
            "\nhetsoc: board(s) wedged during %s and were recovered by "
            "--auto-por.\nStopping anyway: the POR cleared the link state, the "
            "role lock and every CAM rule, so nothing after this point would be "
            "measuring what it claims to.\n" % request.node.nodeid)
        return

    state.report = "\n".join([
        "",
        "=" * 74,
        " HETSOC: BENCH WEDGED — session aborted",
        "=" * 74,
        " triggering test : %s" % request.node.nodeid,
        " boot-ROM probe  : %s" % ", ".join("%s=%s" % kv
                                            for kv in probes.items()),
        " health          : %s" % "; ".join("%s: %s" % kv for kv in
                                            state.snapshot_all().items()),
        "",
        " The PS AXI bus on %s has hung with no timeout. This is the known"
        % ", ".join(dead),
        " intermittent cross-die wedge: the shipped bitstream carries the",
        " UPSTREAM, recovery-stripped FCSM on the five AXI data-plane FC nodes,",
        " so one bit error or dropped ACK has no recovery path and the far",
        " side's B (write) or R (read) beat never returns.",
        "   root cause : docs/CROSS_DIE_WEDGE_ROOTCAUSE.md",
        "   recovery   : JTAG POR — run it ON mapstone-dev:",
        "                  ssh mapstone-dev 'fpgahub board reset %s --yes'"
        % dead[0],
        "                then redeploy and re-run with --deploy.",
        "=" * 74,
        "",
    ])


# ===========================================================================
# Config and boards
# ===========================================================================

@pytest.fixture(scope="session")
def config():
    """The loaded hetsoc config, or a skip when the bench is not configured.

    Discovery order is the framework's: $HETSOC_CONFIG, ./hetsoc.toml,
    ~/.config/hetsoc.toml. L0 needs none of this.
    """
    try:
        cfg = hetsoc_config.load()
    except Exception as exc:                               # noqa: BLE001
        pytest.skip("no usable hetsoc config (%s: %s). The hardware levels need "
                    "a hetsoc.toml describing the boards — see "
                    "host/API_CONTRACT.md 'Config'. L0 runs without one."
                    % (type(exc).__name__, exc))
    if not cfg.board_names():
        pytest.skip("the hetsoc config defines no [board.*] sections, so there "
                    "is no bench to test against. L0 runs with no hardware.")
    return cfg


@pytest.fixture(scope="session")
def available_board_names(request, config):
    """Board names usable this session: `--boards` if given, else all of them.

    A pair needing a board outside this set skips rather than fails — which is
    how the heterogeneous pair opts itself out until a compute bitstream exists.
    """
    configured = set(config.board_names())
    option = request.config.getoption("boards")
    if not option:
        return configured
    wanted = {name.strip() for name in option.split(",") if name.strip()}
    unknown = wanted - configured
    if unknown:
        pytest.fail("--boards names %s, which the hetsoc config does not define "
                    "(it has %s)" % (sorted(unknown), sorted(configured)),
                    pytrace=False)
    return wanted


@pytest.fixture(scope="session")
def fpgahub_lease(config, available_board_names):
    """Hold an fpgahub lease on every available board for the whole session.

    Board-scoped fpgahub routes (`board/<name>/...`) 404 from this host and only
    answer on mapstone-dev; `lease` and `status` work from anywhere. The
    framework's `hetsoc.fpgahub` owns that workaround.
    """
    names = sorted({config.boards[name].get("fpgahub", name)
                    for name in available_board_names})
    try:
        manager = hetsoc_fpgahub.lease(*names)
    except Exception as exc:                               # noqa: BLE001
        pytest.skip("could not lease %s from fpgahub (%s: %s). Check `fpgahub "
                    "status`; per-board routes must be run on mapstone-dev."
                    % (names, type(exc).__name__, exc))
    with manager as leased:
        yield leased


@pytest.fixture(scope="session")
def board_factory(request, config, available_board_names, fpgahub_lease):
    """Build (and cache) a Board by config name, with the leases already held."""
    cache = {}

    def _build(name):
        if name not in available_board_names:
            pytest.skip(
                "board %r is not available this session (available: %s). If this "
                "is the compute chiplet, it has no KR260 bitstream — see "
                "docs/BRINGUP_GAPS.md."
                % (name, sorted(available_board_names)))
        if name not in cache:
            board = config.board(name)
            _state(request.config).register(board)
            cache[name] = board
        return cache[name]

    return _build


def pytest_generate_tests(metafunc):
    """Parameterise every pair-level test over the requested pairs.

    Today that is the homogeneous eth pair. When the compute bitstream exists the
    same tests run over the heterogeneous pair with no edit: every address comes
    from each board's own Target, and `ChipletPair.map_peer_to()` takes the CAM
    replace byte from the RECEIVING die.
    """
    if "pair_name" in metafunc.fixturenames:
        names = list(metafunc.config.getoption("pair")) or ["default"]
        metafunc.parametrize("pair_name", names, scope="session")


@pytest.fixture(scope="session")
def pair_name():
    return "default"


@pytest.fixture(scope="session")
def pair(config, pair_name, board_factory):
    """The two-board pair named by `--pair` (default: `[pair.default]`).

    Skips — never fails — when the pair is not runnable, so a bench that only has
    the eth boards reports "not available" rather than a wall of errors.
    """
    from hetsoc.pair import ChipletPair

    if pair_name not in config.pair_names():
        pytest.skip("the hetsoc config has no [pair.%s]; it defines %s"
                    % (pair_name, config.pair_names() or "no pairs"))
    spec = config.pairs[pair_name]
    a = board_factory(spec["a"])
    b = board_factory(spec["b"])

    for board in (a, b):
        if not board.target.resolved:
            pytest.skip(
                "board %r uses target %r, which has NO verified PS address "
                "window and refuses to translate any address. Reason: %s"
                % (board.name, board.target.name,
                   board.target.provisional_reason or "unresolved"))
        if board.target.peer_aperture < 0:
            pytest.skip(
                "board %r uses target %r, which declares no peer aperture, so it "
                "cannot originate a cross-die access. %s"
                % (board.name, board.target.name,
                   board.target.provisional_reason or ""))

    chiplet_pair = ChipletPair(a, b)
    if chiplet_pair.heterogeneous:
        print("hetsoc: pair %r is HETEROGENEOUS: %s(%s peer=0x%02X) <-> "
              "%s(%s peer=0x%02X). No heterogeneous pair has run on silicon."
              % (pair_name, a.name, a.target.name, a.target.peer_aperture,
                 b.name, b.target.name, b.target.peer_aperture))
    return chiplet_pair


@pytest.fixture(scope="session")
def board_a(pair):
    """Die A of the pair — master / grandmaster (effective_role 0)."""
    return pair.a


@pytest.fixture(scope="session")
def board_b(pair):
    """Die B of the pair — slave (effective_role 1)."""
    return pair.b


@pytest.fixture(params=["a", "b"], ids=["die_a", "die_b"])
def each_board(request, pair):
    """Every single-board test runs against BOTH dies of the pair."""
    return getattr(pair, request.param)


@pytest.fixture(scope="session")
def linked_pair(request, pair):
    """A pair whose link is UP (FCSM=4 + cal_done on both dies).

    This is how the L3 -> L4 ordering dependency is expressed: data-plane tests
    depend on this fixture, not on file order.

    With `--deploy` it reflashes both dies and runs the concurrent bring-up.
    Without it, it only *verifies* — deliberately. Re-running the bring-up on an
    already-live link desyncs it and hangs the sender's peer writes.
    """
    if request.config.getoption("deploy"):
        pair.bringup(deploy=True)
    if not pair.verify_link():
        states = "; ".join(
            "%s fcsm=%d cal=%d" % (b.name, b.lane_status().fcsm,
                                   b.lane_status().cal_done)
            for b in pair.boards)
        pytest.skip(
            "the link is not up on this pair (%s; need fcsm=%d + cal_done on "
            "BOTH dies). Re-run with --deploy to reflash both dies and bring the "
            "link up from fresh. The suite will NOT bring up a link it did not "
            "deploy: LL_SWRESET on a live link desyncs it."
            % (states, regs.FCSM_LINK_IDLE))
    return pair


# ===========================================================================
# Observability and opt-in gates
# ===========================================================================

@pytest.fixture
def health_snapshot():
    """Factory: `health_snapshot(board) -> dict` — a fully decoded, read-only
    health sample (FCSM, cal_done, lane_fault, cr/crack, sticky faults, credits,
    sync_detected, and per-node Wlink FC CRC / Ack-Nack state).

    Everything is decoded, so tests assert on values and never on log text. All
    reads are RO and in-window, so it is safe on a live *or* a down link, and
    safe to call between cross-die transfers — which is exactly where the
    per-node FC registers must be sampled, since they are the only visibility
    into the nodes that wedge.
    """
    return lambda board: board.health()


@pytest.fixture
def soak_iters(request):
    return request.config.getoption("soak_iters")


@pytest.fixture
def allow_peer_read(request):
    """Gate for the peer READ round-trip — the single most wedge-prone access on
    current silicon. An intermittent read-return hang wedged BOTH boards on
    2026-07-29."""
    if not request.config.getoption("allow_peer_read"):
        pytest.skip("the peer READ round-trip is opt-in: pass --allow-peer-read "
                    "(with --data-plane). The write path is already proven "
                    "wedge-safely by a LOCAL read on the receiver.")
    return True


@pytest.fixture
def link_health_guard(pair, request):
    """Post-condition for cross-die tests: the link must still be up on both dies
    afterwards.

    A transfer that "passed" but left the link down has not passed. Runs even
    when the test body failed, so the report carries the link state at the moment
    of failure.
    """
    yield
    for board in pair.boards:
        status = board.lane_status()
        assert status.link_up, (
            "the link is DOWN on %s after %s: %r (need fcsm=%d, cal_done=1). The "
            "transfer disturbed the link — treat any data-plane verdict from "
            "this test as void."
            % (board.name, request.node.name, status, regs.FCSM_LINK_IDLE))
