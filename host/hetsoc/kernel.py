# =============================================================================
# hetsoc.kernel — H5: the reference spectral kernel and its scorer.
#
# WHY THIS IS THE ORACLE
# ----------------------
# S0 runs this kernel host-side while the M4 is stubbed. From S2 on, the M4 runs
# its own fixed-point version and THIS becomes the golden model the hardware is
# scored against. Without it the demonstration proves only that bytes moved, not
# that the right answer came back — and "bytes moved" is already covered by the
# data-plane tests.
#
# Pure Python, no NumPy. `hetsoc` ships with zero runtime dependencies (see
# host/pyproject.toml) and the boards run a plain Ubuntu with no scientific
# stack. A 256-point DFT is ~0.5 ms here via an iterative radix-2 FFT, which is
# irrelevant next to a millisecond-scale cross-die round trip.
#
# ON TOLERANCE
# ------------
# The M4 kernel is Q15 fixed-point; this reference is float. They will not agree
# bit-for-bit and demanding that they do would be a bug in the scorer, not in
# the hardware. `compare()` therefore takes an explicit tolerance and reports
# WHERE the disagreement is, because "band 6 is 3% out" and "every band is
# zero" need different responses.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""Reference spectral kernel (H5) — the oracle S0 scores hardware against."""
from __future__ import annotations

import cmath
import math
from typing import List, NamedTuple, Sequence

from .jobring import KERNEL_BANDS8, N_SAMPLES, Job, Result

__all__ = ["fft", "rfft_magnitude", "bands8", "reference", "Score", "compare",
           "swept_tone", "DEFAULT_REL_TOL"]

# Q15 round-trip through a 256-point transform loses a few LSBs; 2% relative
# with a small absolute floor is loose enough not to cry wolf and tight enough
# that a wrong kernel, a byte-swap or a shifted window all fail it.
DEFAULT_REL_TOL = 0.02
DEFAULT_ABS_TOL = 8.0


def fft(x: Sequence[complex]) -> List[complex]:
    """Iterative radix-2 FFT. len(x) must be a power of two."""
    n = len(x)
    if n & (n - 1):
        raise ValueError("FFT length %d is not a power of two" % n)
    a = list(x)
    # bit-reversal permutation
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    length = 2
    while length <= n:
        ang = -2.0 * math.pi / length
        wl = cmath.exp(1j * ang)
        for i in range(0, n, length):
            w = 1.0 + 0j
            for k in range(i, i + length // 2):
                u = a[k]
                v = a[k + length // 2] * w
                a[k] = u + v
                a[k + length // 2] = u - v
                w *= wl
        length <<= 1
    return a


def rfft_magnitude(samples: Sequence[int]) -> List[float]:
    """Magnitude spectrum of a real signal: N/2 + 1 bins."""
    spec = fft([complex(s, 0.0) for s in samples])
    return [abs(c) for c in spec[: len(samples) // 2 + 1]]


def bands8(mag: Sequence[float]) -> List[int]:
    """Sum the magnitude spectrum into 8 contiguous bands, clamped to uint16.

    DC (bin 0) is EXCLUDED. A DC offset on the ADC would otherwise dominate
    band 0 and swamp the feature it is meant to carry — and a swept-tone test
    signal would still look fine, so the mistake would survive to the bench.
    """
    usable = list(mag[1:])
    per = max(1, len(usable) // 8)
    out = []
    for b in range(8):
        chunk = usable[b * per: (b + 1) * per] if b < 7 else usable[7 * per:]
        out.append(min(0xFFFF, int(round(sum(chunk)))))
    return out


def reference(job: Job, cycles: int = 0) -> Result:
    """Run the reference kernel over a job and produce its Result record."""
    if job.kernel_id != KERNEL_BANDS8:
        raise NotImplementedError(
            "the reference implements kernel_id=%d (BANDS8); job asked for %d. "
            "Add it here before scoring hardware against it — a missing oracle "
            "must fail loudly, not silently score everything as correct."
            % (KERNEL_BANDS8, job.kernel_id))
    return Result(job_id=job.job_id, kernel_id=job.kernel_id, status=0,
                  cycles=cycles, bands=bands8(rfft_magnitude(job.samples)))


class Score(NamedTuple):
    """The verdict of comparing a hardware result against the oracle."""
    ok: bool
    worst_band: int
    worst_rel: float
    detail: str

    def __bool__(self) -> bool:
        return self.ok


def compare(expected: Result, actual: Result,
            rel_tol: float = DEFAULT_REL_TOL,
            abs_tol: float = DEFAULT_ABS_TOL) -> Score:
    """Score a hardware result against the reference.

    Checks identity before numbers: a result carrying the wrong job_id has
    almost certainly been read out of a stale ring slot, and scoring its bands
    would produce a confident answer to the wrong question.
    """
    if actual.job_id != expected.job_id:
        return Score(False, -1, float("inf"),
                     "job_id mismatch: expected %d, got %d — the result was "
                     "probably read from a stale ring slot, so the band values "
                     "are meaningless. Check cons_idx and the publish order."
                     % (expected.job_id, actual.job_id))
    if actual.kernel_id != expected.kernel_id:
        return Score(False, -1, float("inf"),
                     "kernel_id mismatch: expected %d, got %d"
                     % (expected.kernel_id, actual.kernel_id))
    if actual.status != 0:
        return Score(False, -1, float("inf"),
                     "the far die reported status=%d" % actual.status)

    worst_band, worst_rel = -1, 0.0
    for i, (e, a) in enumerate(zip(expected.bands, actual.bands)):
        if abs(a - e) <= abs_tol:
            continue
        rel = abs(a - e) / max(1.0, float(abs(e)))
        if rel > worst_rel:
            worst_band, worst_rel = i, rel
    if worst_rel > rel_tol:
        return Score(False, worst_band, worst_rel,
                     "band %d differs by %.1f%% (expected %d, got %d); "
                     "tolerance is %.1f%% + %g absolute"
                     % (worst_band, 100.0 * worst_rel,
                        expected.bands[worst_band], actual.bands[worst_band],
                        100.0 * rel_tol, abs_tol))
    if all(b == 0 for b in actual.bands) and any(b != 0 for b in expected.bands):
        return Score(False, -1, float("inf"),
                     "every band read back as zero while the oracle expects "
                     "signal — this is the signature of an unwritten or "
                     "unreachable result slot, not of a bad kernel.")
    return Score(True, worst_band, worst_rel,
                 "match (worst band %d at %.2f%%)" % (worst_band, 100.0 * worst_rel))


def swept_tone(n: int = N_SAMPLES, amplitude: int = 8000) -> List[int]:
    """A deterministic synthetic block for S0: a linear chirp, int16.

    Deterministic on purpose — a random block makes a failing run impossible to
    replay, and replay is most of debugging a bench.
    """
    out = []
    for i in range(n):
        f = 0.02 + 0.20 * (i / float(n))          # normalised, sweeps upward
        v = int(round(amplitude * math.sin(2.0 * math.pi * f * i)))
        out.append(max(-32768, min(32767, v)))
    return out
