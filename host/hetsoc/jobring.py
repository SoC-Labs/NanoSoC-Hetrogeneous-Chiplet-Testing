# =============================================================================
# hetsoc.jobring — the S0 job/result wire format and shared-SRAM ring.
#
# Implements docs/SYSTEM_APPLICATION_PROPOSAL.md §3.1 (the L2 job frame) and
# §3.2 (the shared-SRAM ring). Pure host logic: it builds and parses bytes and
# computes offsets. It touches no board, so every line here is testable offline
# and the same code serves the S0 host-driven stage and the later firmware
# stages unchanged.
#
# THE ONE THING THAT WILL BITE
# ----------------------------
# The bottom 256 B of the compute die's shared_sram_0 is NOT free. The M4
# BootROM and the M0+ manager both write there on every power-on
# (compute_mem.h:102-160): 'mgr!' 'SPL!' 'STG1' 'LOCK' 'IRQ!' at +0x00..0x14,
# the M4 CMD/bootcnt/'ARMD' words at +0x40/44/48, the D2D linkup/xfer flags at
# +0x50/54, BootROM breadcrumbs at +0xEC/F0/FC. The 'MRST'/'ARMD' pair is the
# mechanism the shipped app uses for software-mediated M4 reset.
#
# A ring based at +0x0000 would silently corrupt the boot handshake — and the
# symptom would be a compute die that stops booting, days later, blamed on
# something else. RING_BASE is 0x0100 for that reason and RESERVED_BYTES is
# asserted against it at import time.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""S0 job/result wire format and shared-SRAM ring layout."""
from __future__ import annotations

import struct
from typing import List, NamedTuple, Optional

__all__ = [
    "MAGIC", "ETHERTYPE", "N_SAMPLES", "PAYLOAD_BYTES", "JOB_HDR_BYTES",
    "FRAME_BYTES", "RESERVED_BYTES", "RING_BASE", "SLOT_STRIDE", "RING_DEPTH",
    "RESULT_BASE", "RESULT_BYTES", "SHARED_SRAM_BYTES",
    "KERNEL_RFFT256_Q15", "KERNEL_RFFT256_MAG", "KERNEL_BANDS8",
    "FLAG_LAST_IN_BURST", "FLAG_ECHO_INPUT", "DESC_VALID",
    "Job", "Result", "RingLayout", "build_frame", "parse_frame",
]

# --- §3.1 the L2 job frame ---------------------------------------------------
MAGIC = b"NSOC"
ETHERTYPE = 0x88B5            # IEEE 802 Local Experimental EtherType 1
N_SAMPLES = 256
PAYLOAD_BYTES = N_SAMPLES * 2  # 256 x int16 LE
JOB_HDR_BYTES = 16
# §3.1 puts the payload at offset 30, but the named header fields end at 28 —
# so the 16 B header carries 2 B of PAD at +14. Without it every sample is
# shifted two bytes and the frame comes out 540 B instead of 542.
JOB_HDR_PAD = 2
FRAME_BYTES = 14 + JOB_HDR_BYTES + PAYLOAD_BYTES   # 542, pre-FCS
WIRE_BYTES = FRAME_BYTES + 4                       # 546 with the MAC's FCS

KERNEL_RFFT256_Q15 = 0
KERNEL_RFFT256_MAG = 1
KERNEL_BANDS8 = 2

FLAG_LAST_IN_BURST = 1 << 0
FLAG_ECHO_INPUT = 1 << 1

# --- §3.2 the shared-SRAM ring ----------------------------------------------
SHARED_SRAM_BYTES = 8 * 1024   # SHARED_SRAM_RAM_ADDR_W=13
RESERVED_BYTES = 0x0100        # boot-flow signature map — DO NOT OVERLAP
RING_BASE = 0x0100             # CONTROL block
SLOT0_BASE = 0x0110
SLOT_STRIDE = 0x220            # 544 B: 16 B descriptor + 512 B payment + pad
RING_DEPTH = 4
RESULT_BASE = 0x0990
RESULT_BYTES = 32

DESC_VALID = 1 << 0

assert RING_BASE >= RESERVED_BYTES, (
    "the ring must start above the boot-flow signature map at +0x00..0xFF")


class Job(NamedTuple):
    """One unit of work, as it appears on the wire and in a ring descriptor."""
    job_id: int
    kernel_id: int
    n_samples: int
    t_ingress_ns: int
    samples: List[int]          # signed 16-bit
    flags: int = 0

    def descriptor(self) -> bytes:
        """The 16 B in-SRAM descriptor. VALID is set last by the writer, so it
        is deliberately NOT included here — see RingLayout.slot_offsets()."""
        return struct.pack("<IHBBHI2x", DESC_VALID, self.job_id & 0xFFFF,
                           self.kernel_id & 0xFF, self.flags & 0xFF,
                           self.n_samples & 0xFFFF,
                           self.t_ingress_ns & 0xFFFF_FFFF)

    def payload(self) -> bytes:
        if len(self.samples) != self.n_samples:
            raise ValueError("n_samples=%d but %d samples supplied"
                             % (self.n_samples, len(self.samples)))
        return struct.pack("<%dh" % self.n_samples, *self.samples)


class Result(NamedTuple):
    """The 32 B result record the compute die writes to its own SRAM."""
    job_id: int
    kernel_id: int
    status: int
    cycles: int
    bands: List[int]            # 8 x uint16 band energies

    # Named fields occupy 24 B; the record is budgeted at 32. The remaining 8 B
    # are RESERVED and zero-filled rather than left to whatever the M4 last had
    # in that memory — a reader must be able to tell "the producer wrote this"
    # from "this slot is stale".
    _RESERVED = 8

    def pack(self) -> bytes:
        if len(self.bands) != 8:
            raise ValueError("expected 8 band energies, got %d" % len(self.bands))
        return struct.pack("<HBBI8H%dx" % self._RESERVED, self.job_id & 0xFFFF,
                           self.kernel_id & 0xFF, self.status & 0xFF,
                           self.cycles & 0xFFFF_FFFF,
                           *[b & 0xFFFF for b in self.bands])

    @classmethod
    def unpack(cls, raw: bytes) -> "Result":
        if len(raw) < 24:
            raise ValueError("result record too short: %d B (need 24 B of "
                             "named fields)" % len(raw))
        job_id, kern, status, cycles = struct.unpack_from("<HBBI", raw, 0)
        bands = list(struct.unpack_from("<8H", raw, 8))
        return cls(job_id, kern, status, cycles, bands)


def build_frame(job: Job, dst_mac: bytes, src_mac: bytes) -> bytes:
    """The full pre-FCS L2 frame. The MAC appends the CRC (CRCEN in MODER)."""
    if len(dst_mac) != 6 or len(src_mac) != 6:
        raise ValueError("MAC addresses must be 6 bytes")
    hdr = struct.pack("<4sHBBHI2x", MAGIC, job.job_id & 0xFFFF,
                      job.kernel_id & 0xFF, job.flags & 0xFF,
                      job.n_samples & 0xFFFF, job.t_ingress_ns & 0xFFFF_FFFF)
    frame = dst_mac + src_mac + struct.pack(">H", ETHERTYPE) + hdr + job.payload()
    if len(frame) != FRAME_BYTES:
        raise AssertionError("frame is %d B, spec says %d"
                             % (len(frame), FRAME_BYTES))
    return frame


def parse_frame(frame: bytes) -> Optional[Job]:
    """Inverse of build_frame. None if this is not one of our frames."""
    if len(frame) < FRAME_BYTES:
        return None
    if struct.unpack_from(">H", frame, 12)[0] != ETHERTYPE:
        return None
    if frame[14:18] != MAGIC:
        return None
    job_id, kern, flags, n, t_ns = struct.unpack_from("<HBBHI", frame, 18)
    samples = list(struct.unpack_from("<%dh" % n, frame, 30))
    return Job(job_id, kern, n, t_ns, samples, flags)


class RingLayout:
    """Offsets within a die's shared_sram_0. Pure arithmetic, no I/O.

    All offsets are SoC-local (i.e. relative to shared_sram_0's base). The
    sender turns them into peer-aperture addresses; the receiver reads them
    die-locally. Keeping this class ignorant of both is what lets the same
    layout describe either end.
    """

    def __init__(self, depth: int = RING_DEPTH, stride: int = SLOT_STRIDE):
        self.depth = depth
        self.stride = stride

    # --- control block ---
    @property
    def ctrl_magic(self) -> int:
        return RING_BASE + 0x0

    @property
    def ctrl_prod_idx(self) -> int:
        return RING_BASE + 0x4

    @property
    def ctrl_cons_idx(self) -> int:
        return RING_BASE + 0x8

    @property
    def ctrl_config(self) -> int:
        return RING_BASE + 0xC

    def config_word(self) -> int:
        return (self.depth & 0xFF) | ((self.stride & 0xFFFF) << 8)

    # --- slots ---
    def slot_base(self, idx: int) -> int:
        if not 0 <= idx < self.depth:
            raise IndexError("slot %d outside ring depth %d" % (idx, self.depth))
        return SLOT0_BASE + idx * self.stride

    def slot_desc(self, idx: int) -> int:
        return self.slot_base(idx)

    def slot_payload(self, idx: int) -> int:
        return self.slot_base(idx) + 0x10

    def result_slot(self, idx: int) -> int:
        if not 0 <= idx < self.depth:
            raise IndexError("result %d outside ring depth %d" % (idx, self.depth))
        return RESULT_BASE + idx * RESULT_BYTES

    # --- invariants worth asserting rather than trusting ---
    def top_used(self) -> int:
        return RESULT_BASE + self.depth * RESULT_BYTES

    def validate(self) -> None:
        """Raise if the layout collides with the boot map or overruns the SRAM.

        Called by the harness before the first write of a session. The failure
        it exists to prevent is silent: a ring that overlaps the boot signature
        map corrupts the M4 handshake and shows up as a compute die that stops
        booting, long after the write that did it.
        """
        if SLOT0_BASE < RESERVED_BYTES:
            raise ValueError("slot 0 at +0x%04X overlaps the reserved boot map "
                             "(+0x00..0x%02X)" % (SLOT0_BASE, RESERVED_BYTES - 1))

        last_job_end = self.slot_base(self.depth - 1) + self.stride

        # Order matters: "it does not fit in the SRAM at all" is the more
        # fundamental failure and must be reported as such. Checking the
        # internal collision first would tell someone asking for a 64-deep ring
        # that it "overruns the result ring", which is true but useless — the
        # actual problem is that it is eight times the size of the memory.
        needed = max(last_job_end, self.top_used())
        if needed > SHARED_SRAM_BYTES:
            raise ValueError("layout needs 0x%04X B but shared_sram_0 is only "
                             "0x%04X B" % (needed, SHARED_SRAM_BYTES))

        if last_job_end > RESULT_BASE:
            raise ValueError(
                "job ring (depth %d x stride 0x%X, ends +0x%04X) overruns the "
                "result ring at +0x%04X" % (self.depth, self.stride,
                                            last_job_end, RESULT_BASE))

    def slot_offsets(self, idx: int):
        """(descriptor, payload) offsets. The descriptor carries VALID, so the
        caller must write the PAYLOAD FIRST and the descriptor last — otherwise
        the receiver can observe a valid descriptor pointing at stale data."""
        return self.slot_desc(idx), self.slot_payload(idx)
