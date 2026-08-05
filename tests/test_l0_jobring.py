"""L0-RING — the S0 job/result wire format and shared-SRAM ring, offline.

Copyright (C) 2026, SoC Labs (www.soclabs.org)

S0 (docs/SYSTEM_APPLICATION_PROPOSAL.md) is the first stage of the end-to-end
application: a host-driven job/result loop over the two PS backdoors, with the
kernel stubbed host-side. Everything it needs on the host is pure byte and
offset arithmetic, so it is fully testable with no board — which is the point
of writing it now, while the compute rebuild that unblocks the bench is
pending.

The tests that matter most here are the ones guarding the SILENT failures:
a ring that overlaps the boot signature map, and a descriptor published before
its payload.
"""
import struct

import pytest

from hetsoc import jobring
from hetsoc.jobring import Job, Result, RingLayout

pytestmark = pytest.mark.l0


def _samples(n=jobring.N_SAMPLES):
    # A swept tone, as S0 specifies for synthetic blocks. Deterministic, and
    # spans negative values so sign handling is actually exercised.
    return [((i * i) % 2003) - 1000 for i in range(n)]


def _job(**kw):
    base = dict(job_id=0x1234, kernel_id=jobring.KERNEL_BANDS8,
                n_samples=jobring.N_SAMPLES, t_ingress_ns=0xDEADBEEF,
                samples=_samples(), flags=jobring.FLAG_LAST_IN_BURST)
    base.update(kw)
    return Job(**base)


# ===========================================================================
# L0-RING-01..03 — the wire format
# ===========================================================================
def test_l0_ring_01_frame_is_exactly_the_specified_size():
    """L0-RING-01: the pre-FCS frame is 546 B, per §3.1.

    Not cosmetic. The MAC appends a 4 B FCS, and MAXFL bounds the total; a
    frame that has silently grown is the kind of thing that works on loopback
    and fails on a wire.
    """
    frame = jobring.build_frame(_job(), b"\x02\x00\x00\x00\x00\x01",
                                b"\x02\x00\x00\x00\x00\x02")
    assert len(frame) == 542 == jobring.FRAME_BYTES   # pre-FCS
    assert jobring.WIRE_BYTES == 546                  # the MAC appends 4 B
    assert struct.unpack_from(">H", frame, 12)[0] == 0x88B5
    assert frame[14:18] == b"NSOC"


def test_l0_ring_02_frame_round_trips():
    """L0-RING-02: parse(build(job)) preserves every field, signs included."""
    job = _job()
    got = jobring.parse_frame(
        jobring.build_frame(job, b"\xaa" * 6, b"\xbb" * 6))
    assert got is not None
    assert (got.job_id, got.kernel_id, got.n_samples, got.t_ingress_ns,
            got.flags) == (job.job_id, job.kernel_id, job.n_samples,
                           job.t_ingress_ns, job.flags)
    assert got.samples == job.samples
    assert min(got.samples) < 0, "the fixture must exercise negative samples"


def test_l0_ring_03_foreign_frames_are_rejected_not_misparsed():
    """L0-RING-03: a frame that is not ours returns None rather than garbage.

    The eth die will see broadcast and ARP traffic. Misparsing one into a job
    would push arbitrary bytes across the die-to-die link.
    """
    good = jobring.build_frame(_job(), b"\xaa" * 6, b"\xbb" * 6)
    wrong_ethertype = good[:12] + struct.pack(">H", 0x0800) + good[14:]
    wrong_magic = good[:14] + b"XXXX" + good[18:]
    assert jobring.parse_frame(wrong_ethertype) is None
    assert jobring.parse_frame(wrong_magic) is None
    assert jobring.parse_frame(good[:100]) is None      # truncated
    assert jobring.parse_frame(b"") is None


def test_l0_ring_04_result_record_round_trips():
    """L0-RING-04: the 32 B result record survives pack/unpack."""
    r = Result(job_id=0x4321, kernel_id=jobring.KERNEL_BANDS8, status=0,
               cycles=123456, bands=[i * 1000 for i in range(8)])
    got = Result.unpack(r.pack())
    assert got == r
    assert len(r.pack()) == jobring.RESULT_BYTES


# ===========================================================================
# L0-RING-05..07 — the layout, and the silent failures it must prevent
# ===========================================================================
def test_l0_ring_05_ring_never_overlaps_the_boot_signature_map():
    """L0-RING-05: ★ the ring must clear +0x00..0xFF on the compute die.

    THE SILENT ONE. The M4 BootROM and the M0+ manager write 'mgr!' 'SPL!'
    'STG1' 'LOCK' 'IRQ!' 'ARMD' and the BootROM breadcrumbs into the bottom
    256 B at every power-on (compute_mem.h:102-160), and the 'MRST'/'ARMD'
    pair is how the shipped app does software-mediated M4 reset.

    A ring based at +0x0000 corrupts that handshake. The symptom is a compute
    die that stops booting — appearing days later, attributed to anything but
    the cross-die write that caused it.
    """
    lay = RingLayout()
    lay.validate()
    assert jobring.RING_BASE >= jobring.RESERVED_BYTES
    for i in range(lay.depth):
        assert lay.slot_desc(i) >= jobring.RESERVED_BYTES
        assert lay.slot_payload(i) >= jobring.RESERVED_BYTES
        assert lay.result_slot(i) >= jobring.RESERVED_BYTES


def test_l0_ring_06_validate_catches_an_overrunning_ring():
    """L0-RING-06: validate() must reject a layout that does not fit.

    An assertion that cannot fire is decoration. A depth-8 ring at the shipped
    stride overruns the result region, and a 64-deep one overruns the 8 KB SRAM
    entirely; both must be refused rather than silently wrapping onto the
    result records or off the end of memory.
    """
    with pytest.raises(ValueError, match="overruns the result ring"):
        RingLayout(depth=8).validate()
    with pytest.raises(ValueError, match="shared_sram_0 is only"):
        RingLayout(depth=64, stride=0x400).validate()
    with pytest.raises(IndexError):
        RingLayout().slot_base(4)          # depth is 4: valid indices 0..3


def test_l0_ring_07_slots_are_contiguous_and_non_overlapping():
    """L0-RING-07: consecutive slots are exactly one stride apart and disjoint."""
    lay = RingLayout()
    for i in range(lay.depth - 1):
        assert lay.slot_base(i + 1) - lay.slot_base(i) == lay.stride
        # payload of slot i must end before slot i+1 begins
        assert lay.slot_payload(i) + jobring.PAYLOAD_BYTES <= lay.slot_base(i + 1)
    assert lay.slot_base(lay.depth - 1) + lay.stride <= jobring.RESULT_BASE


def test_l0_ring_08_descriptor_carries_valid_and_is_written_last():
    """L0-RING-08: the publish order is payload-then-descriptor.

    The descriptor carries VALID, so writing it before the payload lets the
    receiver observe a valid slot pointing at stale samples. The API expresses
    the ordering (slot_offsets returns descriptor first so the caller writes it
    last); this pins the invariant the ordering exists to protect.
    """
    lay = RingLayout()
    desc, payload = lay.slot_offsets(0)
    assert payload > desc, ("the payload must sit ABOVE the descriptor so the "
                            "descriptor can be the last word written")
    raw = _job().descriptor()
    assert struct.unpack_from("<I", raw, 0)[0] & jobring.DESC_VALID
    assert len(raw) == 16


def test_l0_ring_09_payload_length_is_enforced():
    """L0-RING-09: a short sample block is refused, not silently padded."""
    with pytest.raises(ValueError, match="n_samples"):
        _job(samples=_samples(10)).payload()
    with pytest.raises(ValueError, match="8 band"):
        Result(1, 0, 0, 0, [1, 2, 3]).pack()
