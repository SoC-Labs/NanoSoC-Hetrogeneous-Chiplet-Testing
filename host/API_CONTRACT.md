# `hetsoc` host API contract  (integrator-owned — do not change unilaterally)

The framework (`host/hetsoc/`) and the test suite (`tests/`) are written against
this contract. Both sides must honour it exactly. Extend it freely with *extra*
methods; do not rename or re-signature what is below.

```python
# hetsoc.targets ------------------------------------------------------------
class Target:
    """Per-bitstream address descriptor. Knows how to turn a SoC-internal
    address into a PS physical address, and refuses anything unsafe."""
    name: str                       # e.g. "kr260-eth-chiplet"
    window_base: int                # PS phys base of the SoC backdoor window
    window_size: int
    inbound_targets: dict[str, int] # {"shared_sram": 0x2D, "ipc_mailbox": 0x23}
    peer_aperture: int              # e.g. 0x2F

    def to_host(self, soc_addr: int) -> int: ...
        # returns PS phys addr; raises AddressGuardError if out of window
    def is_peer(self, soc_addr: int) -> bool: ...

TARGETS: dict[str, Target]          # registry; KeyError on unknown name
def get_target(name: str) -> Target: ...

# hetsoc.regs ---------------------------------------------------------------
# SoC-internal offsets, shared across targets (base is per-target).
TLAPB_BASE, SWI_LANE_STATUS, ROLE_CFG, ROLE_STATUS, CREDIT_COUNT, STATUS,
OBS_FC_CREDIT, CAM_BASE, CAM_CTRL, CAM_RULE_0, ...
FCSM_LINK_IDLE = 4
def cam_rule(match_byte: int, replace_byte: int, enable: bool = True) -> int: ...
def decode_lane_status(v: int) -> LaneStatus:  ...   # .fcsm .cal_done .link_up

# hetsoc.board --------------------------------------------------------------
class Board:
    """One KR260. All access goes through the target's guard."""
    def __init__(self, host: str, target: str|Target, role: str, name: str = ""): ...
    name: str; role: str; target: Target
    def read(self, soc_addr: int) -> int: ...
    def write(self, soc_addr: int, value: int) -> None: ...
    def read_many(self, soc_addr: int, n: int) -> list[int]: ...
    def alive(self) -> bool: ...            # boot-ROM probe; never wedges
    def lane_status(self) -> LaneStatus: ...
    def link_up(self) -> bool: ...
    def deploy(self) -> None: ...           # reflash this board's bitstream
    def por(self) -> None: ...              # JTAG POR recovery via fpgahub

# hetsoc.pair ---------------------------------------------------------------
class ChipletPair:
    """The two-board heterogeneous pair."""
    def __init__(self, a: Board, b: Board): ...
    a: Board; b: Board
    def bringup(self, deploy: bool = False) -> None: ...  # concurrent, both dies
    def verify_link(self) -> bool: ...      # read-only FCSM==4 on both
    def peer_write(self, src: Board, soc_addr: int, value: int) -> None: ...
    def program_cam(self, board: Board, match: int, replace: int,
                    enable: bool = True) -> None: ...
    def health(self, board: Board) -> dict: ...   # credits, sticky faults, CRC

# hetsoc.safety -------------------------------------------------------------
class AddressGuardError(Exception): ...
class LinkDownError(Exception): ...
class WedgeDetected(Exception): ...
def require_link_up(board) -> None: ...
def guarded(timeout_s: float): ...          # decorator: turn a hang into WedgeDetected

# hetsoc.fpgahub ------------------------------------------------------------
def lease(*board_names) -> ContextManager: ...
def reset(board_name: str) -> None: ...     # runs on mapstone-dev (404 workaround)
def status() -> dict: ...
```

## Config

Boards/targets come from `hetsoc.config.load()` reading, in order:
`$HETSOC_CONFIG`, `./hetsoc.toml`, `~/.config/hetsoc.toml`. Schema:

```toml
[pair.default]
a = "eth"
b = "compute"

[board.eth]
host   = "ubuntu@10.22.24.159"
fpgahub = "kr260_01"
target = "kr260-eth-chiplet"
role   = "die_a"

[board.compute]
host   = "ubuntu@10.22.24.153"
fpgahub = "kr260_02"
target = "kr260-compute-chiplet"
role   = "die_b"
```

## Non-negotiables

1. `Target.to_host()` **fails loud** on any address outside the window. There is
   no unchecked path to `/dev/mem`.
2. Any peer-aperture access (the `0x2F` window) **must** call
   `require_link_up()` first — a peer access on a down link hangs the PS bus.
3. Every board operation is **timeout-wrapped**; a hang raises `WedgeDetected`,
   never blocks forever.
4. L0 tests import the whole package with **no** board, no `pynq`, no `/dev/mem`.
   Keep hardware access behind lazy imports.
