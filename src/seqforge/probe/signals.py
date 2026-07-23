"""Tier A structural signals — computed from a bounded sample, with no KB and no role assignment.

Every function here reports *structure* (composition, segmentation, recurrence, header grammar,
integrity). None of it assigns a role — ``constant`` is "a constant span", never "the TSO". That
interpretation is the resolver's job.
"""

from __future__ import annotations

import math
import re
from typing import Literal

import numpy as np

from ..models.observation import (
    ConstantSegment,
    CycleComposition,
    HomopolymerSegment,
    RandomSegment,
    ReadLengthProfile,
    ReadNameGrammar,
    Segment,
    WindowDistinctRatio,
)

_BASE_IDX = {"A": 0, "C": 1, "G": 2, "T": 3}
_PURE_THRESHOLD = 0.9  # a cycle whose dominant base fraction >= this is "constant sequence"
_HOMOPOLYMER_MIN = (
    4  # a run of >= this many identical dominant bases is a homopolymer, not a linker
)
_SRA_HEADER = re.compile(r"^[SED]RR\d+\.\d+")
_ILLUMINA_INDEX = re.compile(r"[ /]([ACGTN]{6,})(?:\+([ACGTN]{6,}))?\s*$")


def per_cycle_composition(seqs: list[str]) -> list[CycleComposition]:
    """Fraction of A/C/G/T/N at each 0-based cycle, over reads long enough to reach that cycle.

    Vectorized (numpy) but arithmetically identical to the plain per-base loop it replaces: the counts
    are exact integer column-reductions and the fractions are the same Python ``int / int`` divisions,
    so every :class:`CycleComposition` — and therefore the observation hash — is byte-for-byte the same.
    This was ~79% of a full-size probe (issue #66); it is the dominant per-read cost and the cleanest to
    lift out of Python, which matters most on the explicit large-``--max-reads`` path.
    """
    if not seqs:
        return []
    max_len = max(len(s) for s in seqs)
    if max_len == 0:  # every read empty -> no cycles, same as the loop's empty range
        return []
    # Pack reads into a padded (n_reads x max_len) uint8 matrix of ASCII codes. 0 is the pad sentinel:
    # never a FASTQ base byte, so a column's non-zero cells are exactly the reads that reach that cycle
    # (== the loop's per-cycle `denom`). `encode("ascii", "replace")` maps any non-ASCII to '?' (one
    # byte), which is non-ACGT and lands in the N bucket — exactly as `_BASE_IDX.get(ch, 4)` did.
    arr = np.zeros((len(seqs), max_len), dtype=np.uint8)
    for j, s in enumerate(seqs):
        b = s.encode("ascii", "replace")
        arr[j, : len(b)] = np.frombuffer(b, dtype=np.uint8)
    denom = (arr != 0).sum(axis=0)  # reads reaching each cycle
    a = (arr == 0x41).sum(axis=0)  # 'A'
    c = (arr == 0x43).sum(axis=0)  # 'C'
    g = (arr == 0x47).sum(axis=0)  # 'G'
    t = (arr == 0x54).sum(axis=0)  # 'T'
    nb = denom - (a + c + g + t)  # covered but non-ACGT (incl. N) -> the N bucket
    out: list[CycleComposition] = []
    for i in range(max_len):
        d = int(denom[i]) or 1  # the loop's `denom[i] or 1` guard, preserved
        out.append(
            CycleComposition(
                cycle=i,
                a=int(a[i]) / d,
                c=int(c[i]) / d,
                g=int(g[i]) / d,
                t=int(t[i]) / d,
                n=int(nb[i]) / d,
            )
        )
    return out


def _dominant(comp: CycleComposition) -> tuple[str, float]:
    """Return the dominant ACGT base and its fraction for one cycle."""
    pairs = (("A", comp.a), ("C", comp.c), ("G", comp.g), ("T", comp.t))
    base, frac = max(pairs, key=lambda p: p[1])
    return base, frac


def _entropy_bits(comp: CycleComposition) -> float:
    """Shannon entropy (bits) of the ACGT distribution at one cycle; ~2.0 for uniform."""
    total = comp.a + comp.c + comp.g + comp.t
    if total <= 0:
        return 0.0
    bits = 0.0
    for p in (comp.a, comp.c, comp.g, comp.t):
        q = p / total
        if q > 0:
            bits -= q * math.log2(q)
    return bits


def segment(comps: list[CycleComposition]) -> list[Segment]:
    """Merge cycles into constant / homopolymer / random segments (structural, role-free).

    A cycle whose dominant base fraction >= ``_PURE_THRESHOLD`` is "constant sequence". Within a run
    of pure cycles, a run of the same dominant base (>= ``_HOMOPOLYMER_MIN``) is a homopolymer (polyT
    capture / polyA tail); a stretch of varying pure bases is a linker/adapter/TSO (constant).
    Everything else is random (CB/UMI/cDNA candidate). Index == cycle by construction.
    """
    if not comps:
        return []
    labels: list[tuple[str, str, float]] = []  # (kind, dominant_base, purity) per cycle
    for comp in comps:
        base, frac = _dominant(comp)
        labels.append(("pure", base, frac) if frac >= _PURE_THRESHOLD else ("random", base, frac))

    segments: list[Segment] = []
    i = 0
    n = len(labels)
    while i < n:
        kind = labels[i][0]
        j = i + 1
        while j < n and labels[j][0] == kind:
            j += 1
        if kind == "random":
            mean_bits = sum(_entropy_bits(comps[k]) for k in range(i, j)) / (j - i)
            segments.append(RandomSegment(start=i, end=j, mean_entropy_bits=mean_bits))
        else:
            segments.extend(_split_pure_run(labels, i, j))
        i = j
    return segments


def _split_pure_run(labels: list[tuple[str, str, float]], lo: int, hi: int) -> list[Segment]:
    """Split a run of pure cycles ``[lo, hi)`` into homopolymer + constant (linker) segments."""
    out: list[Segment] = []
    const_start: int | None = None

    def flush_constant(end: int) -> None:
        nonlocal const_start
        if const_start is None:
            return
        bases = [labels[k][1] for k in range(const_start, end)]
        purity = sum(labels[k][2] for k in range(const_start, end)) / (end - const_start)
        out.append(
            ConstantSegment(start=const_start, end=end, consensus="".join(bases), purity=purity)
        )
        const_start = None

    k = lo
    while k < hi:
        base = labels[k][1]
        r = k + 1
        while r < hi and labels[r][1] == base:
            r += 1
        if r - k >= _HOMOPOLYMER_MIN:
            flush_constant(k)
            out.append(
                HomopolymerSegment(
                    base=base,  # type: ignore[arg-type]  # single ACGT char
                    start=k,
                    end=r,
                    mean_run=float(r - k),
                )
            )
        elif const_start is None:
            const_start = k
        k = r
    flush_constant(hi)
    return out


def read_length_profile(seqs: list[str]) -> ReadLengthProfile:
    """Mode, distinct-count, min/max, and (only when variable) percentiles of read length."""
    lengths = sorted(len(s) for s in seqs)
    if not lengths:
        return ReadLengthProfile(mode=0, n_distinct=1, min_len=0, max_len=0)
    freq: dict[int, int] = {}
    for length in lengths:
        freq[length] = freq.get(length, 0) + 1
    mode = max(freq, key=lambda k: freq[k])
    n_distinct = len(freq)
    percentiles = None
    if n_distinct > 1:
        percentiles = {
            "p1": lengths[max(0, (len(lengths) * 1) // 100 - 1)],
            "p50": lengths[len(lengths) // 2],
            "p99": lengths[min(len(lengths) - 1, (len(lengths) * 99) // 100)],
        }
    return ReadLengthProfile(
        mode=mode,
        n_distinct=n_distinct,
        min_len=lengths[0],
        max_len=lengths[-1],
        percentiles=percentiles,
    )


def window_distinct_ratio(seqs: list[str], start: int, end: int) -> float | None:
    """distinct/total over an explicit ``[start, end)`` window (role-conditioned; used by resolve)."""
    window = [s[start:end] for s in seqs if len(s) >= end]
    if not window:
        return None
    return len(set(window)) / len(window)


def distinct_ratios(seqs: list[str], segments: list[Segment]) -> list[WindowDistinctRatio]:
    """distinct/total over each random segment (candidate CB/UMI/cDNA window). Supports-only signal."""
    out: list[WindowDistinctRatio] = []
    for seg in segments:
        if not isinstance(seg, RandomSegment):
            continue
        window = [s[seg.start : seg.end] for s in seqs if len(s) >= seg.end]
        if not window:
            continue
        ratio = len(set(window)) / len(window)
        out.append(
            WindowDistinctRatio(
                start=seg.start, end=seg.end, distinct_ratio=ratio, n_sampled=len(window)
            )
        )
    return out


def parse_read_name(name: str | None) -> ReadNameGrammar:
    """Parse an Illumina header; detect SRA-normalized headers (the index has been stripped)."""
    if not name:
        return ReadNameGrammar(parsed=False)
    if _SRA_HEADER.match(name):
        return ReadNameGrammar(parsed=False, sra_normalized=True)
    fields = name.split(":")
    if len(fields) >= 7:
        index_match = _ILLUMINA_INDEX.search(name)
        index = index_match.group(1) if index_match else None
        lane: int | None
        tile: int | None
        try:
            lane = int(fields[3])
            tile = int(fields[4])
        except ValueError:
            lane = None
            tile = None
        return ReadNameGrammar(
            parsed=True,
            instrument=fields[0],
            run=fields[1],
            flowcell=fields[2],
            lane=lane,
            tile=tile,
            index=index,
        )
    return ReadNameGrammar(parsed=False)


def quality_encoding(
    min_ord: int | None, max_ord: int | None
) -> Literal["phred33", "phred64", "unknown"]:
    """Infer the Phred offset from the observed quality-char ordinal range."""
    if min_ord is None or max_ord is None:
        return "unknown"
    if min_ord < 64:
        return "phred33"
    if max_ord > 74:
        return "phred64"
    return "unknown"


def n_rate(seqs: list[str]) -> float:
    """Fraction of non-ACGT (N) bases across the sample."""
    total = 0
    n_count = 0
    for s in seqs:
        total += len(s)
        for ch in s:
            if ch not in _BASE_IDX:
                n_count += 1
    return (n_count / total) if total else 0.0
