"""``WindowProbe`` — the bounded, role-conditioned view the resolver scores against (§3.1).

The structural :class:`Observation` is deliberately role-free and carries no raw sequences, but the
resolver needs role-conditioned answers — distinct-ratio and onlist hit-rate over *arbitrary*
``[start, end)`` windows a candidate technology proposes. ``WindowProbe`` pairs the Observation with
the same bounded, in-memory sample that produced it (from :func:`probe.probe_sample`) and answers
those window queries. It never re-reads the file: the sample is already within the budget.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..io import HitResult, Orientation, PackedOnlist, onlist_hit_rate
from ..models.observation import CycleComposition, Observation

_IUPAC = {
    "A": "A", "C": "C", "G": "G", "T": "T",
    "R": "AG", "Y": "CT", "S": "GC", "W": "AT", "K": "GT", "M": "AC",
    "B": "CGT", "D": "AGT", "H": "ACT", "V": "ACG", "N": "ACGT",
}  # fmt: skip


@dataclass(frozen=True)
class WindowProbe:
    """An Observation plus its bounded sampled sequences, queryable over arbitrary windows."""

    observation: Observation
    seqs: list[str]

    @property
    def n_sampled(self) -> int:
        return len(self.seqs)

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
