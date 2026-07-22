"""``WindowProbe`` — the bounded, role-conditioned view the resolver scores against (§3.1).

The structural :class:`Observation` is deliberately role-free and carries no raw sequences, but the
resolver needs role-conditioned answers — distinct-ratio and onlist hit-rate over *arbitrary*
``[start, end)`` windows a candidate technology proposes. ``WindowProbe`` pairs the Observation with
the same bounded, in-memory sample that produced it (from :func:`probe.probe_sample`) and answers
those window queries. It never re-reads the file: the sample is already within the budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..io import HitResult, Orientation, PackedOnlist, onlist_hit_rate
from ..io.onlist import pack_barcode, revcomp
from ..kb.schema import Read
from ..models.observation import CycleComposition, Observation

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
            from ..kb.anchor import resolve_windows

            cached = [resolve_windows(s, read) for s in self.seqs]
            self._frame_cache[key] = cached
        return cached

    def anchored_windows(self, read: Read, element_name: str) -> list[tuple[int, int] | None]:
        """The per-read ``[start, end)`` of one floating element (``None`` where the frame was lost)."""
        return [f.get(element_name) if f is not None else None for f in self._frames(read)]

    def anchored_distinct_ratio(self, read: Read, element_name: str) -> float | None:
        """``distinct/total`` of a floating element's per-read slices; ``None`` if no frame resolved."""
        windows = self.anchored_windows(read, element_name)
        slices = [
            self.seqs[i][s:e] for i, w in enumerate(windows) if w is not None for s, e in (w,)
        ]
        slices = [s for s in slices if s]
        if not slices:
            return None
        return len(set(slices)) / len(slices)

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
        whose frame resolved to a window of the onlist's width; a lost frame simply does not contribute.
        """
        windows = self.anchored_windows(read, element_name)
        strands = (
            ["forward"]
            if orientation == "forward"
            else ["revcomp"]
            if orientation == "revcomp"
            else ["forward", "revcomp"]
        )
        best = HitResult(
            hit_rate=0.0, orientation="forward", offset=0, n_tested=0, floor=onlist.floor
        )
        for strand in strands:
            tested = 0
            hits = 0
            for i, w in enumerate(windows):
                if w is None:
                    continue
                sub = self.seqs[i][w[0] : w[1]]
                if len(sub) != onlist.width:
                    continue
                tested += 1
                code = pack_barcode(revcomp(sub) if strand == "revcomp" else sub)
                if code is not None and onlist.contains(code):
                    hits += 1
            if tested and hits / tested > best.hit_rate:
                best = HitResult(
                    hit_rate=hits / tested,
                    orientation=strand,  # type: ignore[arg-type]
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
        window = [s[start:end] for s in self.seqs if len(s) >= end]
        if not window:
            return None
        return len(set(window)) / len(window)

    def composition_window(self, start: int, end: int | None) -> list[CycleComposition]:
        """Per-cycle composition over cycles ``[start, end)`` (``end=None`` => to the longest read)."""
        comps = self.observation.per_cycle_composition
        stop = len(comps) if end is None else min(end, len(comps))
        return [c for c in comps[start:stop]]

    def onlist_hit(
        self, start: int, onlist: PackedOnlist, orientation: Orientation = "either"
    ) -> HitResult:
        """Best whitelist hit anchored at ``start`` (width from the onlist), fwd + revcomp + offset scan."""
        return onlist_hit_rate(self.seqs, start, onlist, orientation=orientation)

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
