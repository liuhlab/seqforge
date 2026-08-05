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
    HeadCoverage,
    HomopolymerSegment,
    RandomSegment,
    ReadLengthProfile,
    ReadNameGrammar,
    Segment,
    WindowDistinctRatio,
)

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

    The per-cycle denominator is *reported*, not just used: ``n_sampled`` carries the column's own
    ``denom``, which every coverage figure downstream reduces. Computing it costs nothing here — it
    is the reduction the fractions were already divided by — and it cannot be recovered later from a
    fraction.
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
                n_sampled=int(denom[i]),  # the true count, never the divide-by-zero guard
            )
        )
    return out


def _called_cells(comps: list[CycleComposition], lo: int, hi: int) -> int:
    """Cells over cycles ``[lo, hi)`` that carried a called base: reads reaching, minus uncalled.

    Recovered from the fractions rather than re-counted, so the head is walked once. Both factors
    came from exact ``int / int`` divisions of the same column, so the product is an integer that has
    made one round trip through a double; ``round`` returns it rather than trusting the last bit.
    """
    return sum(round(c.n_sampled * (1.0 - c.n)) for c in comps[lo:hi])


def _span_coverage(comps: list[CycleComposition], lo: int, hi: int, n_reads: int) -> float:
    """Share of the head's ``(read, cycle)`` cells over ``[lo, hi)`` that carried a called base.

    The denominator is every sampled read across the whole span — the material that *could* have
    contributed — so both ways a cell goes missing are counted: a read too short to reach the cycle,
    and a base the sequencer never called. A span nothing could contribute to reads ``0.0``.

    Nothing clamps the result: a called-cell count above the cells that exist is arithmetically
    impossible, so the ``Confidence`` bound on the field refusing it is the report we want, not a
    quietly capped number.
    """
    possible = n_reads * (hi - lo)
    if possible <= 0:
        return 0.0
    return _called_cells(comps, lo, hi) / possible


def head_coverage(comps: list[CycleComposition], n_reads: int) -> HeadCoverage:
    """Split the head's overall coverage into its two loss channels — reach, then base call.

    ``n_reads`` is the head's own sampled-read count (what the observation reports as
    ``probe.n_reads_sampled``), so the denominator is the sample the caller will see beside the
    figure rather than a count re-derived here. The two multiply to the head's overall coverage —
    the same quantity each segment reports over its own span — and
    :class:`~seqforge.models.observation.HeadCoverage` argues why they are reported apart, and why
    nothing reads either.
    """
    possible = n_reads * len(comps)
    reached = sum(c.n_sampled for c in comps)
    if possible <= 0 or reached <= 0:  # no cells at all: an empty head covers nothing
        return HeadCoverage(reach_fraction=0.0, called_fraction=0.0)
    return HeadCoverage(
        reach_fraction=reached / possible,
        called_fraction=_called_cells(comps, 0, len(comps)) / reached,
    )


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


def segment(comps: list[CycleComposition], n_reads: int) -> list[Segment]:
    """Merge cycles into constant / homopolymer / random segments (structural, role-free).

    A cycle whose dominant base fraction >= ``_PURE_THRESHOLD`` is "constant sequence". Within a run
    of pure cycles, a run of the same dominant base (>= ``_HOMOPOLYMER_MIN``) is a homopolymer (polyT
    capture / polyA tail); a stretch of varying pure bases is a linker/adapter/TSO (constant).
    Everything else is random (CB/UMI/cDNA candidate). Index == cycle by construction.

    ``n_reads`` is the head's sampled-read count and is the coverage denominator, nothing else: each
    segment records what share of the sampled material its span was classified over. That is the one
    thing a classification cannot say about itself — a dominant-base fraction of 0.08 looks like
    "random" whether the cycle is genuinely random or 92% uncalled — and no consumer decides anything
    from it.
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
            segments.append(
                RandomSegment(
                    start=i,
                    end=j,
                    mean_entropy_bits=mean_bits,
                    coverage=_span_coverage(comps, i, j, n_reads),
                )
            )
        else:
            segments.extend(_split_pure_run(labels, comps, i, j, n_reads))
        i = j
    return segments


def _split_pure_run(
    labels: list[tuple[str, str, float]],
    comps: list[CycleComposition],
    lo: int,
    hi: int,
    n_reads: int,
) -> list[Segment]:
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
            ConstantSegment(
                start=const_start,
                end=end,
                consensus="".join(bases),
                purity=purity,
                coverage=_span_coverage(comps, const_start, end, n_reads),
            )
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
                    # `_dominant` picks this out of a literal ACGT 4-tuple; it is typed `str`.
                    base=base,  # type: ignore[arg-type]
                    start=k,
                    end=r,
                    mean_run=float(r - k),
                    coverage=_span_coverage(comps, k, r, n_reads),
                )
            )
        elif const_start is None:
            const_start = k
        k = r
    flush_constant(hi)
    return out


def read_length_profile(seqs: list[str]) -> ReadLengthProfile:
    """Mode, its share of the reads, distinct-count, min/max, and (when variable) percentiles.

    ``mode_share`` is the share of the sampled reads sitting at the modal length -- the population
    statement ``n_distinct`` cannot make, since counting which lengths are present says nothing about
    how the reads divide among them. Every read has a length, so the denominator is the whole sample
    and an empty head reports 0.0 rather than a vacuous 1.0.
    """
    lengths = sorted(len(s) for s in seqs)
    if not lengths:
        return ReadLengthProfile(mode=0, n_distinct=1, min_len=0, max_len=0, mode_share=0.0)
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
        mode_share=freq[mode] / len(lengths),
        percentiles=percentiles,
    )


def window_bases(seqs: list[str], start: int, end: int) -> list[str]:
    """The bases each read carries in the fixed column ``[start, end)``.

    A read shorter than ``end`` does not contain the column at all, so it contributes nothing --
    never a partial slice, which would compare a short string against full-width ones. The anchored
    twin is :func:`seqforge.kb.anchor.element_bases`, which cuts a per-read frame instead and guards
    a different failure (a resolved window of zero width).
    """
    return [s[start:end] for s in seqs if len(s) >= end]


def distinct_ratio(bases: list[str]) -> float | None:
    """distinct/total over already-cut bases; ``None`` when nothing was cut.

    Deliberately dumb: it counts what it is handed and never decides which reads contribute. That is
    the cutter's job, because *which* bases to cut is role-conditioned (a KB spec, or probe's own
    segmentation, decided the range meant something) while this arithmetic never is.
    """
    if not bases:
        return None
    return len(set(bases)) / len(bases)


def modal_consensus(bases: list[str]) -> str | None:
    """The column-wise most common base over already-cut reads; ``None`` when nothing was cut.

    What :func:`consensus_match_rate` measures agreement *against*, returned rather than consumed. The
    rate answers "do the reads agree here"; this answers "on WHAT" — and the second question had no
    answer anywhere, which is why the round-trip could prove a spec's linker window was constant
    without ever proving it held the sequence the spec declares.

    Deliberately dumb in the same way as its neighbours: it counts what it is handed and decides
    nothing about which reads contribute. A column no read reaches is all pad, and pad is not a base,
    so it falls to ``A`` by argmax — harmless where every cut is the same width (a fixed column, or an
    element cut at a resolved frame), which is every caller there is.
    """
    arr = _padded(bases)
    if arr is None:
        return None
    return _consensus(arr).tobytes().decode("ascii")


def _padded(bases: list[str]) -> np.ndarray | None:
    """``bases`` as a right-padded ``(n, width)`` uint8 matrix; ``None`` when there is nothing to read.

    0 is the pad sentinel -- never a base byte, so it never equals a consensus base and a read that
    falls short of the column counts as a non-carrier rather than a partial match.
    """
    if not bases:
        return None
    width = max(len(b) for b in bases)
    if width == 0:
        return None
    arr = np.zeros((len(bases), width), dtype=np.uint8)
    for j, b in enumerate(bases):
        raw = b.encode("ascii", "replace")
        arr[j, : len(raw)] = np.frombuffer(raw, dtype=np.uint8)
    return arr


def _consensus(arr: np.ndarray) -> np.ndarray:
    """The modal base of each column, as ASCII codes. One owner, because two callers now read it."""
    counts = np.stack([(arr == ord(base)).sum(axis=0) for base in "ACGT"])
    return np.frombuffer(b"ACGT", dtype=np.uint8)[counts.argmax(axis=0)]


def consensus_match_rate(bases: list[str], max_mismatch: int) -> float | None:
    """The SHARE of already-cut reads within ``max_mismatch`` of the column-wise modal consensus.

    "Is a fixed sequence here" is a question about a population of reads, and its honest form is a
    proportion: how many of them carry it. A mean over per-cycle purities answers a different
    question, and cannot tell *every read carries this linker* from *most do and the rest of the head
    is junk* -- the two agree only on a head with no junk in it, which is the one kind of head a
    generated fixture ever has.

    **Counted, never selected.** A read that does not carry the sequence stays in the denominator, so
    contamination lowers this honestly. Filtering to the reads that agree with the consensus and then
    measuring their agreement would return ~1.0 for any window in any dataset, pure noise included --
    a statistic that cannot fail, which is worse than one calibrated too tight. Uniform-random bases
    put this at ~0 for any window wide enough to have a consensus worth the name: matching 30 columns
    to within 3 by chance is ~1e-13.

    ``None`` when nothing was cut. Like :func:`distinct_ratio`, deliberately dumb -- it counts what it
    is handed.

    **Its denominator is the reads that REACH the column**, because :func:`window_bases` drops a read
    too short to span it before this ever sees it. So on a ragged file this is "of the reads long
    enough to carry this sequence, how many do", not "of all reads" -- and the share reads higher than
    a whole-file share would. That is the right split of labour rather than a lost case: whether a
    file's reads are long enough to fill a role at all is the declared geometry's question, and
    ``read_length_compatible`` asks it before scoring gets here. Contamination this cannot see is
    contamination the length gate already refused.

    **How many reads that was, is reported elsewhere and not here.** This function is handed a list
    and counts it; the window it came from is role-conditioned, chosen at scoring time, and unknown
    to the probe. The observation records the denominator per cycle instead
    (``CycleComposition.n_sampled``), from which the count for any window is the value at its last
    cycle — one figure that covers every window anyone will ever cut, rather than a coverage number
    this function would have to invent a window to report.
    """
    arr = _padded(bases)
    if arr is None:
        return None
    mismatches = (arr != _consensus(arr)).sum(axis=1)
    return float((mismatches <= max_mismatch).mean())


def distinct_ratios(seqs: list[str], segments: list[Segment]) -> list[WindowDistinctRatio]:
    """distinct/total over each random segment (candidate CB/UMI/cDNA window). Supports-only signal."""
    out: list[WindowDistinctRatio] = []
    for seg in segments:
        if not isinstance(seg, RandomSegment):
            continue
        bases = window_bases(seqs, seg.start, seg.end)
        ratio = distinct_ratio(bases)
        if ratio is None:
            continue
        out.append(
            WindowDistinctRatio(
                start=seg.start, end=seg.end, distinct_ratio=ratio, n_sampled=len(bases)
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
