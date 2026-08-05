# =============================================================================
# hetsoc.bit27 — H6: the eth backdoor's bit-27 drop, and its blast radius.
#
# THE DEFECT (gap E-e, characterised 2026-07-31, UNFIXED)
# ------------------------------------------------------
# The PS -> eth_ss_0 backdoor DROPS BIT 27 on some reads. The boot-ROM
# reset/NMI/HardFault vectors read back 0x000001xx where 0x080001xx is
# expected; the init-MSP word in the same region reads clean. It is an
# eth-subsystem aperture bug, orthogonal to the TideLink link
# (eth_ss_probe.py:12-22, I1_RESOLVED_HANDOVER_2026_07_31.md:58,99).
#
# `eth_ss_probe.py` WAIVES a diff that is exactly bit 27 so its aliveness check
# still passes in CI. That waiver is correct for an aliveness probe and wrong
# for a payload check: S0 verifies application data over this same path, so a
# silent bit-27 drop turns into a corrupted sample or a wrong band energy that
# looks like a compute fault.
#
# WHAT THIS MODULE IS FOR
# -----------------------
# Not to fix it — that needs an RTL change nobody has scoped. It is to make the
# defect IMPOSSIBLE TO MISATTRIBUTE:
#
#   * classify()   — is this mismatch the known defect, or something new?
#   * verify()     — a payload check that separates the two in its verdict
#   * sweep_plan() — the characterisation H6 actually asks for, as data
#
# The last matters because the defect is only half-characterised. "Some reads"
# is not a blast radius. Until a sweep says whether it is address- or data-side
# and which regions are affected, every byte-exact verdict over this path
# carries an asterisk, and this module is where that asterisk lives.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""The eth backdoor bit-27 drop (gap E-e): classification and blast radius."""
from __future__ import annotations

from enum import Enum
from typing import List, NamedTuple, Optional, Sequence

__all__ = ["BIT27", "Verdict", "classify", "verify", "SweepPoint", "sweep_plan",
           "summarise_sweep"]

BIT27 = 1 << 27


class Verdict(Enum):
    """What a read-back mismatch means."""
    MATCH = "match"
    KNOWN_BIT27_DROP = "known-bit27-drop"   # exactly E-e: bit 27 lost, nothing else
    OTHER_MISMATCH = "other-mismatch"       # a real defect, or a new one

    def __bool__(self) -> bool:             # truthy only when genuinely equal
        return self is Verdict.MATCH


def classify(expected: int, observed: int) -> Verdict:
    """Classify one 32-bit read-back.

    KNOWN_BIT27_DROP requires the difference to be EXACTLY bit 27 going 1->0.
    Anything else — bit 27 appearing when it should not, or bit 27 dropping
    alongside other bits — is OTHER_MISMATCH. Widening this to "any difference
    involving bit 27" would let a genuine multi-bit corruption hide behind the
    known defect, which is the specific failure this module exists to prevent.
    """
    expected &= 0xFFFF_FFFF
    observed &= 0xFFFF_FFFF
    if expected == observed:
        return Verdict.MATCH
    if (expected ^ observed) == BIT27 and (expected & BIT27):
        return Verdict.KNOWN_BIT27_DROP
    return Verdict.OTHER_MISMATCH


class Check(NamedTuple):
    ok: bool
    n_words: int
    n_bit27: int
    n_other: int
    first_other: Optional[int]      # index of the first genuine mismatch
    detail: str


def verify(expected: Sequence[int], observed: Sequence[int],
           waive_bit27: bool = False) -> Check:
    """Compare a block read back over the eth backdoor.

    `waive_bit27=False` by DEFAULT — the opposite of `eth_ss_probe.py`, and
    deliberately so. That probe is checking the SoC is alive, where a waiver is
    right. This is checking application payload, where a dropped bit is a
    corrupted sample. Callers that genuinely want the aliveness semantics must
    ask for them.
    """
    if len(expected) != len(observed):
        return Check(False, len(expected), 0, 0, None,
                     "length mismatch: expected %d words, observed %d"
                     % (len(expected), len(observed)))
    n_bit27 = n_other = 0
    first_other = None
    for i, (e, o) in enumerate(zip(expected, observed)):
        v = classify(e, o)
        if v is Verdict.KNOWN_BIT27_DROP:
            n_bit27 += 1
        elif v is Verdict.OTHER_MISMATCH:
            n_other += 1
            if first_other is None:
                first_other = i
    ok = n_other == 0 and (waive_bit27 or n_bit27 == 0)

    if n_other:
        detail = ("%d word(s) differ beyond the known bit-27 drop; first at "
                  "index %d (expected 0x%08X, observed 0x%08X). This is NOT "
                  "E-e — do not waive it."
                  % (n_other, first_other, expected[first_other],
                     observed[first_other]))
    elif n_bit27 and not waive_bit27:
        detail = ("%d word(s) lost exactly bit 27 — the known eth-backdoor "
                  "defect E-e (eth_ss_probe.py:12-22). The payload is CORRUPT "
                  "even though the cause is known. Pass waive_bit27=True only "
                  "if this is an aliveness check, not a data check."
                  % n_bit27)
    elif n_bit27:
        detail = "%d bit-27 drop(s), waived by request (E-e)" % n_bit27
    else:
        detail = "%d words verified clean" % len(expected)
    return Check(ok, len(expected), n_bit27, n_other, first_other, detail)


class SweepPoint(NamedTuple):
    """One probe of the characterisation sweep."""
    soc_addr: int
    pattern: int
    note: str


def sweep_plan(base: int, n_words: int = 64) -> List[SweepPoint]:
    """The H6 characterisation sweep, as data rather than a script.

    H6 asks a specific question the existing probe cannot answer: is the drop
    ADDRESS-side (certain addresses always lose bit 27) or DATA-side (any word
    with bit 27 set loses it)? Those have different blast radii and different
    fixes, and "some reads" does not distinguish them.

    The discriminator is patterns that differ ONLY in bit 27 written to the same
    address, and the same pattern at many addresses. Returned as data so the
    caller owns the I/O — this module never touches a board, so it stays
    testable offline.
    """
    plan: List[SweepPoint] = []
    # A: same address, patterns straddling bit 27 -> data-side dependence.
    for pat, note in ((0x0800_0000, "bit 27 alone"),
                      (0x0000_0000, "bit 27 clear, else identical"),
                      (0xFFFF_FFFF, "all ones"),
                      (0xF7FF_FFFF, "all ones except bit 27"),
                      (0x0800_01A5, "the failing boot-vector shape"),
                      (0x0000_01A5, "same, bit 27 pre-cleared")):
        plan.append(SweepPoint(base, pat, note))
    # B: one pattern with bit 27 set, swept across addresses -> address-side.
    for i in range(n_words):
        plan.append(SweepPoint(base + 4 * i, 0x0800_0000 | i,
                               "address sweep word %d" % i))
    return plan


def summarise_sweep(results: Sequence[tuple]) -> str:
    """Turn (SweepPoint, observed) pairs into the address-vs-data verdict.

    Deliberately refuses to guess from thin evidence: if every probed word
    carrying bit 27 drops it, that is data-side; if only some addresses do, it
    is address-side; if neither pattern holds cleanly the answer is 'mixed',
    which is a real possible outcome and more useful than a confident wrong
    label.
    """
    with_b27 = [(p, o) for p, o in results if p.pattern & BIT27]
    if not with_b27:
        return "inconclusive: no probe pattern had bit 27 set"
    dropped = [(p, o) for p, o in with_b27 if classify(p.pattern, o) is
               Verdict.KNOWN_BIT27_DROP]
    clean = [(p, o) for p, o in with_b27 if classify(p.pattern, o) is Verdict.MATCH]
    other = [(p, o) for p, o in with_b27 if classify(p.pattern, o) is
             Verdict.OTHER_MISMATCH]

    if len(dropped) == len(with_b27):
        verdict = ("DATA-SIDE: every probed word carrying bit 27 lost it, at "
                   "every address tried. Blast radius = any value with bit 27 "
                   "set, read over this path.")
    elif not dropped:
        verdict = ("NOT REPRODUCED: no probe lost bit 27. Either the defect is "
                   "narrower than the boot vectors suggest, or this region is "
                   "unaffected — do not conclude it is fixed from one sweep.")
    else:
        addrs = sorted({p.soc_addr for p, _ in dropped})
        verdict = ("ADDRESS-SIDE or MIXED: %d of %d bit-27 words dropped it, "
                   "over %d distinct address(es) starting 0x%08X. Blast radius "
                   "is region-specific; treat byte-exact checks outside those "
                   "regions as unproven rather than safe."
                   % (len(dropped), len(with_b27), len(addrs), addrs[0]))
    if other:
        verdict += ("\n  WARNING: %d probe(s) mismatched in ways that are NOT "
                    "the bit-27 signature — there is a second effect here."
                    % len(other))
    return "%s\n  dropped=%d clean=%d other=%d" % (
        verdict, len(dropped), len(clean), len(other))
