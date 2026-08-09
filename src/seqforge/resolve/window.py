"""``WindowProbe`` — the bounded, role-conditioned view the resolver scores against.

The structural :class:`Observation` is deliberately role-free and carries no raw sequences, but the
resolver needs role-conditioned answers — distinct-ratio and onlist hit-rate over *arbitrary*
``[start, end)`` windows a candidate technology proposes. ``WindowProbe`` pairs the Observation with
the same bounded, in-memory sample that produced it (from :func:`probe.probe_sample`) and answers
those window queries. It never re-reads the file: the sample is already within the budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..io import HitResult, Orientation, PackedOnlist, onlist_hit_rate
from ..io.onlist import STRANDS_SCANNED, pack_barcode, revcomp
from ..kb.anchor import element_bases, resolve_windows
from ..kb.schema import MotifWhere, Read
from ..models.observation import CycleComposition, Observation
from ..probe import signals as sig  # module-qualified: `distinct_ratio` is also a method below

_IUPAC = {
    "A": "A", "C": "C", "G": "G", "T": "T",
    "R": "AG", "Y": "CT", "S": "GC", "W": "AT", "K": "GT", "M": "AC",
    "B": "CGT", "D": "AGT", "H": "ACT", "V": "ACG", "N": "ACGT",
}  # fmt: skip

#: The bases a sequencer calls, read off the table above rather than spelled a second time: `N` is
#: the IUPAC code for "any of them", so its expansion IS the called alphabet. Same predicate the
#: onlist paths apply through `pack_barcode`, which returns `None` on anything outside it.
_CALLED = frozenset(_IUPAC["N"])


@dataclass(frozen=True)
class WindowProbe:
    """An Observation plus its bounded sampled sequences, queryable over arbitrary windows.

    Most windows are a fixed ``[start, end)`` column. **Anchored** (floating) elements are not — their
    per-read position is recovered by :func:`seqforge.kb.anchor.resolve_windows`, and the
    ``anchored_*`` methods answer distinct-ratio / onlist-hit over those per-read frames instead. The
    full per-read frame is resolved once and memoized (``_frame_cache``, keyed by the ``Read`` object)
    so scoring three cell-label blocks on one read does not realign it three times.

    Every method here **cuts, then measures**, and owns neither half: the fixed column is cut by
    :func:`probe.signals.window_bases` and the per-read frame by :func:`kb.anchor.element_bases`, and
    what comes back is measured by :func:`probe.signals.distinct_ratio` or ``io.onlist``. What this
    class contributes is the pairing of an Observation with the bounded head that produced it, and
    the frame cache — not arithmetic.
    """

    observation: Observation
    seqs: list[str]
    #: read object identity -> per-sampled-read resolved element windows (``None`` where the frame was
    #: not found). Mutated in place; the frozen dataclass forbids rebinding the attribute, not filling
    #: the dict. ``compare=False`` so two probes with equal seqs stay equal regardless of what was cached.
    _frame_cache: dict[int, list[dict[str, tuple[int, int]] | None]] = field(
        default_factory=dict, compare=False, repr=False
    )

    @property
    def n_sampled(self) -> int:
        return len(self.seqs)

    def _frames(self, read: Read) -> list[dict[str, tuple[int, int]] | None]:
        """Per-read resolved element windows for an anchored layout, memoized per ``Read``."""
        key = id(read)
        cached = self._frame_cache.get(key)
        if cached is None:
            cached = [resolve_windows(s, read) for s in self.seqs]
            self._frame_cache[key] = cached
        return cached

    def anchored_distinct_ratio(self, read: Read, element_name: str) -> float | None:
        """``distinct/total`` of a floating element's per-read bases; ``None`` if no frame resolved."""
        return sig.distinct_ratio(element_bases(self.seqs, self._frames(read), element_name))

    def anchored_onlist_hit(
        self,
        read: Read,
        element_name: str,
        onlist: PackedOnlist,
        orientation: Orientation = "either",
    ) -> HitResult:
        """Whitelist hit-rate of a floating element, sliced per read at its resolved frame.

        The anchored twin of :func:`~seqforge.io.onlist.onlist_hit_rate`: no offset scan (the frame IS
        the offset), forward and/or reverse-complement per ``orientation``. ``n_tested`` counts reads
        whose frame resolved to a window of the onlist's width **and whose window is all-ACGT** — a
        lost frame does not contribute, and neither does a window the sequencer never called. That is
        one policy shared with the fixed-offset twin, which argues it in full; in one line, a read that
        cannot hit by construction measures the run rather than the library, so it leaves the
        denominator and shows up as lost coverage in ``n_tested`` instead.
        """
        bases = element_bases(self.seqs, self._frames(read), element_name)
        # The same TOTAL mapping the fixed-offset twin uses, rather than a second if/elif/else whose
        # last arm quietly means "scan both strands" for anything it does not recognise. That
        # fallthrough was the live defect in `onlist_hit_rate` (#148); here it is latent, because
        # every caller passes a pydantic-validated `OnlistHitTest.orientation`. Latent is not fixed —
        # it is one untyped caller away, and a wrong orientation costs a thin matrix rather than an
        # error. Sharing the mapping is what stops the two sites drifting: a fourth orientation now
        # fails loudly at both, instead of silently meaning "either" at one of them.
        strands = STRANDS_SCANNED[orientation]
        best = HitResult(
            hit_rate=0.0, orientation="forward", offset=0, n_tested=0, floor=onlist.floor
        )
        for strand in strands:
            tested = 0
            hits = 0
            for sub in bases:
                if len(sub) != onlist.width:
                    continue
                code = pack_barcode(revcomp(sub) if strand == "revcomp" else sub)
                if code is None:
                    # A non-ACGT base in the frame: unpackable, so it cannot hit however good the
                    # whitelist is. It leaves the denominator for the same reason a lost frame does —
                    # neither read says anything about which whitelist these barcodes came from.
                    continue
                tested += 1
                if onlist.contains(code):
                    hits += 1
            if tested and hits / tested > best.hit_rate:
                best = HitResult(
                    hit_rate=hits / tested,
                    orientation=strand,
                    offset=0,
                    n_tested=tested,
                    floor=onlist.floor,
                )
        return best

    @property
    def mode_length(self) -> int:
        return self.observation.read_length.mode

    def distinct_ratio(self, start: int, end: int) -> float | None:
        """``distinct/total`` over ``[start, end)`` (role-conditioned; a supports signal, never a gate)."""
        return sig.distinct_ratio(sig.window_bases(self.seqs, start, end))

    def consensus_match_rate(self, start: int, end: int, max_mismatch: int) -> float | None:
        """Share of reads carrying ``[start, end)``'s modal consensus to within ``max_mismatch``.

        The per-READ companion to :meth:`composition_window`, which can only report cycles already
        aggregated over every sampled read. "How constant is this column on average" and "how many
        reads carry this sequence" are different facts, and only the second one survives a head that
        is part junk — see :func:`probe.signals.consensus_match_rate` for why it is a proportion.
        """
        return sig.consensus_match_rate(sig.window_bases(self.seqs, start, end), max_mismatch)

    def composition_window(self, start: int, end: int | None) -> list[CycleComposition]:
        """Per-cycle composition over cycles ``[start, end)`` (``end=None`` => to the longest read)."""
        comps = self.observation.per_cycle_composition
        stop = len(comps) if end is None else min(end, len(comps))
        return [c for c in comps[start:stop]]

    def onlist_hit(
        self,
        start: int,
        onlist: PackedOnlist,
        orientation: Orientation = "either",
        offset_scan: int = 2,
    ) -> HitResult:
        """Best whitelist hit anchored at ``start`` (width from the onlist), fwd + revcomp + offset scan.

        ``offset_scan`` widens the ± column slide: the default 2 absorbs phasing slack; a barcode behind
        a fixed lead-in (10x Multiome ATAC) sets it wide enough to reach the barcode from ``start``.
        """
        return onlist_hit_rate(
            self.seqs, start, onlist, orientation=orientation, offset_scan=offset_scan
        )

    def motif_rate(
        self,
        motif: str,
        *,
        where: MotifWhere = "anywhere",
        search_start: int | None = None,
        search_end: int | None = None,
        max_mismatch: int = 1,
    ) -> float | None:
        """Fraction of reads matching an IUPAC ``motif`` (<= ``max_mismatch``) in the search window.

        **An uncalled base is not a substitution** — the policy the two onlist paths above already
        share, arrived at here late. A base the sequencer never called is evidence neither for the
        motif nor against it, so a position whose window holds one is skipped rather than scored, and
        a read with nothing left to score leaves the denominator instead of diluting the rate.
        Before this an ``N`` failed the IUPAC membership test like any wrong base: it ate the
        ``max_mismatch`` budget on every read at once, and the rate reported the run rather than the
        library. At a majority ``requires`` threshold that is enough for a dark cycle to take a whole
        family of specs out of the running.

        **It costs nothing where the motif asks nothing.** Only the offsets a motif *constrains* can
        be lost; a cycle under an ``N`` was never evidence, so blanking it leaves the read whole.

        **What it costs elsewhere depends on what the search DECLARES.** ``read_start`` / ``read_end``
        / a closed ``window`` name where the motif is, so their candidate positions are one claim
        staggered rather than independent chances: an uncalled base at a constrained offset of *any*
        of them leaves the read unable to answer, and it leaves ``tested`` whole. ``anywhere`` — and a
        ``window`` left open at the end, which runs just as far — declares nothing, so each position
        is its own chance, only the positions it reaches are lost, and the read leaves ``tested`` only
        when none of them survives.

        A read the declared window does not *fit* is a different fact — a length one, gated on length
        elsewhere — and stays a miss. Only an uncalled base costs coverage.

        ``None`` when no read could be measured at all, which a caller must read as "cannot see"
        rather than "absent". The loss itself is reported by ``Observation.coverage``, which is why
        this returns a bare rate and does not carry its own denominator.
        """
        m = len(motif)
        if m == 0:
            return None
        constrained = _constrained_offsets(motif)
        tested = 0
        matched = 0
        for seq in self.seqs:
            if len(seq) < m:
                continue
            starts, bounded = _search_positions(seq, m, where, search_start, search_end)
            called = _called_starts(seq, starts, constrained)
            if starts and (not called or (bounded and len(called) < len(starts))):
                continue  # never called where the motif was looked for: coverage, not a miss
            tested += 1
            if any(
                _motif_matches(seq[p : p + m], motif, constrained, max_mismatch) for p in called
            ):
                matched += 1
        if tested == 0:
            return None
        return matched / tested


def _constrained_offsets(motif: str) -> tuple[int, ...]:
    """The offsets in ``motif`` that constrain the read — every code but a fully ambiguous one.

    ``N`` accepts all four bases, so a cycle under one is not evidence and was never going to be: it
    cannot mismatch, and a base nobody called there costs nothing. The shipped Enhanced motif is
    ``GTGANNNNNNNNNGACA``, nine of whose seventeen positions are exactly that — masking the whole
    width instead would let a dark cycle in the cell-label block throw away a read still showing both
    linkers intact.
    """
    return tuple(j for j, code in enumerate(motif) if len(_IUPAC.get(code, code)) < len(_CALLED))


def _search_positions(
    seq: str, m: int, where: MotifWhere, search_start: int | None, search_end: int | None
) -> tuple[list[int], bool]:
    """Where a motif of width ``m`` could start in ``seq``, and whether those positions are ONE claim.

    ``where`` is classified **here and nowhere else**. Two sites reading the same field and disagreeing
    about a value neither recognises is the shape that was a live defect in the whitelist scan; a
    single classification cannot drift from itself.

    Positions the read is too short to hold are dropped, so what comes back is what the read could
    actually be asked. An empty list means the read cannot carry the motif where it was looked for —
    a fact about its layout, not about the run.

    ``bounded`` is whether the search **declared a finite span**. ``read_start`` and ``read_end`` name
    one position and ``window`` names both its ends, so their positions are one claim staggered rather
    than independent chances. A ``window`` left open at the end has declared no span at all — it runs
    to wherever the read stops, exactly as ``anywhere`` does — so it is not one claim either.
    """
    lo = (search_start or 0) if where == "window" else 0
    if where == "read_start":
        return _fitting([0], m, len(seq)), True
    if where == "read_end":
        return _fitting([len(seq) - m], m, len(seq)), True
    if where == "window" and search_end is not None:
        return _fitting(list(range(lo, search_end + 1)), m, len(seq)), True
    return _fitting(list(range(lo, len(seq) - m + 1)), m, len(seq)), False


def _fitting(starts: list[int], m: int, n: int) -> list[int]:
    return [p for p in starts if p >= 0 and p + m <= n]


def _called_starts(seq: str, starts: list[int], constrained: tuple[int, ...]) -> list[int]:
    """Those of ``starts`` whose window the sequencer called at every offset the motif constrains.

    Scanning the read for uncalled bases once, rather than every window at every position, is what
    keeps an unbounded search affordable: the common case is a read with none, which costs one pass
    and hands ``starts`` back untouched.
    """
    uncalled = [i for i, base in enumerate(seq) if base not in _CALLED]
    if not uncalled:
        return starts
    offsets = set(constrained)
    return [p for p in starts if all(i - p not in offsets for i in uncalled)]


def _motif_matches(
    window: str, motif: str, constrained: tuple[int, ...], max_mismatch: int
) -> bool:
    """Does ``window`` carry ``motif`` within ``max_mismatch`` substitutions?

    Only the offsets the motif **constrains** are scored, and every base at one of them was called —
    the caller drops the windows that were not. So a base outside its IUPAC code is a substitution
    and nothing else. That is the invariant to keep: an uncalled base re-entering here as a mismatch,
    at a position the motif never asked about, is the defect this no longer has to defend against.
    """
    mism = 0
    for j in constrained:
        if window[j] not in _IUPAC.get(motif[j], motif[j]):
            mism += 1
            if mism > max_mismatch:
                return False
    return True
