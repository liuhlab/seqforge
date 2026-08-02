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
from ..io.onlist import _STRANDS_SCANNED, pack_barcode, revcomp
from ..kb.anchor import element_bases, resolve_windows
from ..kb.schema import Read
from ..models.observation import CycleComposition, Observation
from ..probe import signals as sig  # module-qualified: `distinct_ratio` is also a method below

_IUPAC = {
    "A": "A", "C": "C", "G": "G", "T": "T",
    "R": "AG", "Y": "CT", "S": "GC", "W": "AT", "K": "GT", "M": "AC",
    "B": "CGT", "D": "AGT", "H": "ACT", "V": "ACG", "N": "ACGT",
}  # fmt: skip


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
        strands = _STRANDS_SCANNED[orientation]
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
        where: str = "anywhere",
        search_start: int | None = None,
        search_end: int | None = None,
        max_mismatch: int = 1,
    ) -> float | None:
        """Fraction of reads matching an IUPAC ``motif`` (<= ``max_mismatch``) in the search window."""
        m = len(motif)
        if m == 0:
            return None
        tested = 0
        matched = 0
        for seq in self.seqs:
            if len(seq) < m:
                continue
            tested += 1
            if self._read_has_motif(seq, motif, where, search_start, search_end, max_mismatch):
                matched += 1
        if tested == 0:
            return None
        return matched / tested

    @staticmethod
    def _read_has_motif(
        seq: str,
        motif: str,
        where: str,
        search_start: int | None,
        search_end: int | None,
        max_mismatch: int,
    ) -> bool:
        m = len(motif)
        if where == "read_start":
            starts = [0]
        elif where == "read_end":
            starts = [len(seq) - m]
        elif where == "window":
            lo = search_start or 0
            hi = search_end if search_end is not None else len(seq) - m
            starts = list(range(lo, hi + 1))
        else:  # anywhere
            starts = list(range(0, len(seq) - m + 1))
        for pos in starts:
            if pos < 0 or pos + m > len(seq):
                continue
            if _motif_matches(seq[pos : pos + m], motif, max_mismatch):
                return True
        return False


def _motif_matches(window: str, motif: str, max_mismatch: int) -> bool:
    mism = 0
    for base, code in zip(window, motif, strict=True):
        if base not in _IUPAC.get(code, code):
            mism += 1
            if mism > max_mismatch:
                return False
    return True
