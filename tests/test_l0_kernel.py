"""L0-KERN / L0-B27 — the S0 oracle and the backdoor defect underneath it.

Copyright (C) 2026, SoC Labs (www.soclabs.org)

H5 (`hetsoc.kernel`) is the reference the hardware is scored against. H6
(`hetsoc.bit27`) is the known eth-backdoor defect that sits underneath every
host-side verdict S0 produces. Both are pure host logic and fully testable with
no board.

The tests that carry weight here are the ones proving the SCORER can fail, and
that the bit-27 classifier cannot be used to launder a genuine corruption.
"""
import pytest

from hetsoc import bit27, kernel
from hetsoc.bit27 import BIT27, Verdict
from hetsoc.jobring import KERNEL_BANDS8, KERNEL_RFFT256_MAG, Job

pytestmark = pytest.mark.l0


def _job(samples=None, job_id=7, kernel_id=KERNEL_BANDS8):
    s = samples if samples is not None else kernel.swept_tone()
    return Job(job_id=job_id, kernel_id=kernel_id, n_samples=len(s),
               t_ingress_ns=0, samples=s, flags=0)


# ===========================================================================
# L0-KERN — the oracle
# ===========================================================================
def test_l0_kern_01_fft_matches_a_known_transform():
    """L0-KERN-01: the FFT is correct against hand-computable cases.

    A pure tone at bin k must put essentially all its energy in bin k. If the
    transform were wrong every downstream comparison would still be
    self-consistent — the oracle would simply be confidently wrong — so this is
    checked against arithmetic, not against itself.
    """
    n = 256
    for k in (1, 5, 64):
        sig = [int(10000 * __import__("math").cos(2 * __import__("math").pi * k * i / n))
               for i in range(n)]
        mag = kernel.rfft_magnitude(sig)
        peak = max(range(len(mag)), key=lambda i: mag[i])
        assert peak == k, "tone at bin %d peaked at %d" % (k, peak)
        others = [m for i, m in enumerate(mag) if i != k]
        assert max(others) < 0.05 * mag[k], "energy leaked outside bin %d" % k

    with pytest.raises(ValueError, match="power of two"):
        kernel.fft([1 + 0j] * 100)


def test_l0_kern_02_bands_exclude_dc():
    """L0-KERN-02: a pure DC offset must not dominate band 0.

    A constant offset is exactly what a real ADC front end delivers, and a
    swept-tone test signal would not reveal the mistake — the bug would survive
    to the bench and present as 'band 0 is always saturated'.
    """
    dc_only = [4000] * 256
    assert kernel.bands8(kernel.rfft_magnitude(dc_only)) == [0] * 8

    tone = kernel.swept_tone()
    assert any(b > 0 for b in kernel.bands8(kernel.rfft_magnitude(tone)))


def test_l0_kern_03_reference_is_deterministic_and_typed():
    """L0-KERN-03: same input -> same Result, and it fits the 32 B record."""
    job = _job()
    r1, r2 = kernel.reference(job), kernel.reference(job)
    assert r1 == r2
    assert r1.job_id == job.job_id and r1.status == 0
    assert len(r1.bands) == 8 and all(0 <= b <= 0xFFFF for b in r1.bands)
    assert len(r1.pack()) == 32


def test_l0_kern_04_unknown_kernel_id_fails_loudly():
    """L0-KERN-04: an unimplemented kernel must raise, never score as correct.

    A missing oracle that returns a default would mark every hardware result
    as passing — the worst possible failure for a scorer.
    """
    with pytest.raises(NotImplementedError, match="BANDS8"):
        kernel.reference(_job(kernel_id=KERNEL_RFFT256_MAG))


def test_l0_kern_05_scorer_accepts_q15_noise_but_rejects_real_error():
    """L0-KERN-05: ★ the scorer must be able to FAIL.

    A comparator that passes everything is worse than no comparator. Checks
    three graded cases: exact, small rounding-scale perturbation, and a real
    error — plus the all-zeros case, which is the signature of an unwritten
    result slot rather than a bad kernel and must be called out as such.
    """
    job = _job()
    exp = kernel.reference(job)

    assert kernel.compare(exp, exp).ok

    nudged = exp._replace(bands=[max(0, b - 1) for b in exp.bands])
    assert kernel.compare(exp, nudged).ok, "q15-scale rounding must be tolerated"

    broken = exp._replace(bands=[b // 2 for b in exp.bands])
    s = kernel.compare(exp, broken)
    assert not s.ok and "differs by" in s.detail

    zeros = exp._replace(bands=[0] * 8)
    s = kernel.compare(exp, zeros)
    assert not s.ok
    assert "unwritten or" in s.detail or "differs by" in s.detail


def test_l0_kern_06_scorer_checks_identity_before_numbers():
    """L0-KERN-06: a stale-slot result must be rejected on job_id, not scored.

    Scoring the bands of a result read from the wrong ring slot produces a
    confident answer to the wrong question. Identity first.
    """
    exp = kernel.reference(_job(job_id=7))
    stale = exp._replace(job_id=6)
    s = kernel.compare(exp, stale)
    assert not s.ok and "stale ring slot" in s.detail

    s = kernel.compare(exp, exp._replace(status=3))
    assert not s.ok and "status=3" in s.detail


# ===========================================================================
# L0-B27 — the backdoor defect
# ===========================================================================
def test_l0_b27_01_classifies_only_the_exact_signature():
    """L0-B27-01: ★ the known-defect label must not launder real corruption.

    KNOWN_BIT27_DROP requires the difference to be EXACTLY bit 27 going 1->0.
    Widening it to 'any difference involving bit 27' would let a multi-bit
    corruption hide behind the known defect — precisely the misattribution this
    module exists to prevent.
    """
    assert bit27.classify(0x0800_01A5, 0x0000_01A5) is Verdict.KNOWN_BIT27_DROP
    assert bit27.classify(0x1234_5678, 0x1234_5678) is Verdict.MATCH

    # bit 27 dropped AND another bit changed -> not the known defect
    assert bit27.classify(0x0800_01A5, 0x0000_01A4) is Verdict.OTHER_MISMATCH
    # bit 27 APPEARING is not the known defect either
    assert bit27.classify(0x0000_01A5, 0x0800_01A5) is Verdict.OTHER_MISMATCH
    # a different single bit dropping is not it
    assert bit27.classify(0x0400_01A5, 0x0000_01A5) is Verdict.OTHER_MISMATCH

    assert not Verdict.KNOWN_BIT27_DROP      # truthy only when MATCH
    assert bool(Verdict.MATCH)


def test_l0_b27_02_payload_verification_does_not_waive_by_default():
    """L0-B27-02: a data check must NOT inherit the aliveness probe's waiver.

    eth_ss_probe.py waives bit-27 diffs so its aliveness check passes in CI.
    That is right for 'is the SoC alive' and wrong for 'is my payload intact' —
    a dropped bit is a corrupted sample whatever the cause.
    """
    expected = [0x0800_0001, 0x0000_0002, 0x0800_0003]
    observed = [0x0000_0001, 0x0000_0002, 0x0000_0003]   # two bit-27 drops

    strict = bit27.verify(expected, observed)
    assert not strict.ok and strict.n_bit27 == 2 and strict.n_other == 0
    assert "CORRUPT" in strict.detail

    waived = bit27.verify(expected, observed, waive_bit27=True)
    assert waived.ok and waived.n_bit27 == 2


def test_l0_b27_03_genuine_corruption_is_never_waived():
    """L0-B27-03: waive_bit27 must not suppress a non-E-e mismatch."""
    expected = [0x0800_0001, 0xAAAA_AAAA]
    observed = [0x0000_0001, 0xAAAA_0000]     # one known drop, one real error
    for waive in (False, True):
        chk = bit27.verify(expected, observed, waive_bit27=waive)
        assert not chk.ok, "a real corruption was waived (waive=%s)" % waive
        assert chk.n_other == 1 and chk.first_other == 1
        assert "do not waive" in chk.detail

    assert not bit27.verify([1, 2], [1]).ok    # length mismatch


def test_l0_b27_04_sweep_distinguishes_address_from_data_side():
    """L0-B27-04: the H6 sweep must give a usable verdict, including 'mixed'.

    'Some reads' is not a blast radius. The sweep exists to say whether the
    drop follows the DATA (any word with bit 27) or the ADDRESS (specific
    regions) — different radii, different fixes.
    """
    plan = bit27.sweep_plan(0x1800_0000, n_words=4)
    assert any(p.pattern & BIT27 for p in plan)
    assert len({p.soc_addr for p in plan}) > 1, "the plan must sweep addresses"

    # data-side: every bit-27 word drops it, wherever it is
    data_side = [(p, p.pattern & ~BIT27 if p.pattern & BIT27 else p.pattern)
                 for p in plan]
    assert "DATA-SIDE" in bit27.summarise_sweep(data_side)

    # clean: nothing drops
    assert "NOT REPRODUCED" in bit27.summarise_sweep([(p, p.pattern) for p in plan])

    # mixed: only the first bit-27 word drops
    mixed, hit = [], False
    for p in plan:
        if p.pattern & BIT27 and not hit:
            mixed.append((p, p.pattern & ~BIT27))
            hit = True
        else:
            mixed.append((p, p.pattern))
    out = bit27.summarise_sweep(mixed)
    assert "ADDRESS-SIDE or MIXED" in out

    # a second, non-bit-27 effect must be surfaced, not folded in
    with_other = [(p, 0xDEAD_BEEF) for p in plan]
    assert "second effect" in bit27.summarise_sweep(with_other)
