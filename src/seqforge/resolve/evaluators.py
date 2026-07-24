"""Signature-test evaluators — the CLOSED set that mirrors ``kb.schema`` exactly (§3.1).

``evaluate(test, read, wp, spec, registry)`` returns an :class:`Evaluation` carrying both a gate
``outcome`` (``PASS`` / ``FAIL`` / ``ABSTAIN``) and a supports ``score`` in ``[0, 1]``. The caller
uses ``outcome`` for ``requires`` / ``excludes`` gates and ``score`` for ``supports`` weighting.

Two invariants hold regardless of where a test is placed:
- **``ABSTAIN`` never gates.** "The probe cannot see this signal" is not "the signal is absent" — an
  abstaining requires/excludes test is a pass-through, not a rejection (an SRA-normalized
  ``header_index`` must not reject every SRA dataset).
- **``distinct_ratio`` never gates.** It is depth-dependent: its gate outcome is forced to ``ABSTAIN``
  so a misplaced ``requires`` cannot use it; its supports ``score`` remains meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..io import OnlistNotAvailable, OnlistRegistry
from ..kb.schema import (
    BaseComposition,
    DistinctRatio,
    Element,
    HasSegment,
    HeaderIndex,
    MotifPresent,
    OnlistHitRate,
    Read,
    SegmentLength,
    Spec,
)
from ..models.observation import CycleComposition
from .window import WindowProbe

_CONSTANT_PURITY = 0.9  # mean max-base fraction over a window to call it "constant sequence"
_RANDOM_MAXFRAC = 0.55  # mean max-base fraction below this is "near-uniform random"


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ABSTAIN = "abstain"


@dataclass(frozen=True)
class Evaluation:
    """A test's gate ``outcome`` and its supports ``score`` in ``[0, 1]``, with a short reason."""

    outcome: Outcome
    score: float
    detail: str = ""
    #: True iff a real (materialized) onlist was consulted — lifts the deciding rung to 3.
    used_onlist: bool = False


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _window_for(test: object, read: Read) -> tuple[int, int | None]:
    """Resolve a test's FIXED target window from ``element`` name XOR explicit ``(start, end)``.

    Only valid for fixed-offset elements. A **floating** element has no constant window — its per-read
    frame is resolved by :meth:`WindowProbe.anchored_windows`, and the callers below route to it via
    :func:`_anchored_element` before ever reaching here.
    """
    element = getattr(test, "element", None)
    if element is not None:
        for el in read.elements:
            if el.name == element:
                return (el.start or 0), el.end
        return 0, None
    start = getattr(test, "start", None)
    end = getattr(test, "end", None)
    return (start or 0), end


def _anchored_element(test: object, read: Read) -> Element | None:
    """The floating element a test targets by name, or ``None`` if it targets a fixed one / coordinates.

    This is where ``el.anchor`` — dropped by every consuming layer before #43 — is finally read on the
    scoring path: an ``onlist_hit_rate`` / ``distinct_ratio`` addressed to an anchored element must be
    answered over the per-read frame, not a constant column.
    """
    name = getattr(test, "element", None)
    if name is None:
        return None
    for el in read.elements:
        if el.name == name and el.anchor is not None:
            return el
    return None


def _mean_max_fraction(wp: WindowProbe, start: int, end: int | None) -> float | None:
    comps = wp.composition_window(start, end)
    if not comps:
        return None
    total = 0.0
    for c in comps:
        total += max(c.a, c.c, c.g, c.t)
    return total / len(comps)


def _base_fraction(c: CycleComposition, base: str) -> float:
    return {"A": c.a, "C": c.c, "G": c.g, "T": c.t, "N": c.n}[base]


def _mean_base_fraction(wp: WindowProbe, start: int, end: int | None, base: str) -> float | None:
    comps = wp.composition_window(start, end)
    if not comps:
        return None
    return sum(_base_fraction(c, base) for c in comps) / len(comps)


def evaluate(
    test: object, read: Read, wp: WindowProbe, spec: Spec, registry: OnlistRegistry
) -> Evaluation:
    """Evaluate one signature test against a file's :class:`WindowProbe`."""
    if isinstance(test, SegmentLength):
        return _eval_segment_length(test, wp)
    if isinstance(test, HasSegment):
        return _eval_has_segment(test, read, wp)
    if isinstance(test, DistinctRatio):
        return _eval_distinct_ratio(test, read, wp)
    if isinstance(test, OnlistHitRate):
        return _eval_onlist(test, read, wp, spec, registry)
    if isinstance(test, MotifPresent):
        return _eval_motif(test, wp)
    if isinstance(test, BaseComposition):
        return _eval_base_composition(test, read, wp)
    if isinstance(test, HeaderIndex):
        return _eval_header_index(test, wp)
    # read_count is a dataset-level global, handled by the assignment feasibility check.
    return Evaluation(Outcome.ABSTAIN, 0.0, "not a per-cell test")


def _eval_segment_length(test: SegmentLength, wp: WindowProbe) -> Evaluation:
    mode = wp.mode_length
    if mode == 0:
        return Evaluation(Outcome.FAIL, 0.0, "no reads")
    if test.over_length_min is not None and mode >= test.over_length_min:
        # An over-sequenced / insert-bearing barcode read: CB+UMI live at the declared offsets and the
        # trailing bases are junk STARsolo ignores. Canonical exactness is preserved because
        # over_length_min sits strictly above `length` (a 28 bp read is never "over-length").
        return Evaluation(
            Outcome.PASS, 1.0, f"mode={mode} >= over_length_min={test.over_length_min}"
        )
    diff = abs(mode - test.length)
    outcome = Outcome.PASS if diff <= test.tolerance else Outcome.FAIL
    score = _clip(1.0 - diff / max(1.0, test.length * 0.1))
    return Evaluation(outcome, score, f"mode={mode} vs {test.length}±{test.tolerance}")


def _eval_has_segment(test: HasSegment, read: Read, wp: WindowProbe) -> Evaluation:
    start, end = _window_for(test, read)
    if test.kind in ("constant", "random"):
        mmf = _mean_max_fraction(wp, start, end)
        if mmf is None:
            return Evaluation(Outcome.ABSTAIN, 0.0, "window unreadable")
        if test.kind == "constant":
            outcome = Outcome.PASS if mmf >= _CONSTANT_PURITY else Outcome.FAIL
            return Evaluation(outcome, _clip((mmf - 0.5) / 0.4), f"mean_maxfrac={mmf:.2f}")
        outcome = Outcome.PASS if mmf <= _RANDOM_MAXFRAC else Outcome.FAIL
        return Evaluation(outcome, _clip((_RANDOM_MAXFRAC - mmf) / 0.3), f"mean_maxfrac={mmf:.2f}")
    base = "T" if test.kind == "polyT" else "A"
    frac = _mean_base_fraction(wp, start, end, base)
    if frac is None:
        return Evaluation(Outcome.ABSTAIN, 0.0, "window unreadable")
    outcome = Outcome.PASS if frac >= 0.8 else Outcome.FAIL
    return Evaluation(outcome, _clip(frac), f"{base}-fraction={frac:.2f}")


def _eval_distinct_ratio(test: DistinctRatio, read: Read, wp: WindowProbe) -> Evaluation:
    """SUPPORTS-only: the gate outcome is forced to ABSTAIN so it can never gate (depth-dependent)."""
    anchored = _anchored_element(test, read)
    if anchored is not None:
        ratio = wp.anchored_distinct_ratio(read, anchored.name)
        detail = "anchored "
    else:
        start, end = _window_for(test, read)
        if end is None:
            return Evaluation(Outcome.ABSTAIN, 0.0, "open-ended window")
        ratio = wp.distinct_ratio(start, end)
        detail = ""
    if ratio is None:
        return Evaluation(Outcome.ABSTAIN, 0.0, "window unreadable")
    score = _clip(1.0 - ratio) if test.expect == "low" else _clip(ratio)
    return Evaluation(
        Outcome.ABSTAIN, score, f"{detail}distinct_ratio={ratio:.3f} expect={test.expect}"
    )


def _eval_onlist(
    test: OnlistHitRate, read: Read, wp: WindowProbe, spec: Spec, registry: OnlistRegistry
) -> Evaluation:
    ref = spec.onlists.get(test.onlist)
    if ref is None:
        return Evaluation(Outcome.ABSTAIN, 0.0, f"unknown onlist alias {test.onlist!r}")
    if not registry.has(ref.registry):
        return Evaluation(Outcome.ABSTAIN, 0.0, f"onlist {ref.registry!r} not registered")
    try:
        packed = registry.packed(ref.registry)
    except OnlistNotAvailable:
        return Evaluation(Outcome.ABSTAIN, 0.0, f"onlist {ref.registry!r} not materialized")
    anchored = _anchored_element(test, read)
    if anchored is not None:
        hit = wp.anchored_onlist_hit(read, anchored.name, packed, orientation=test.orientation)
    else:
        start, _ = _window_for(test, read)
        hit = wp.onlist_hit(
            start, packed, orientation=test.orientation, offset_scan=test.offset_scan
        )
    outcome = Outcome.PASS if hit.hit_rate >= test.min else Outcome.FAIL
    detail = f"hit={hit.hit_rate:.2f} min={test.min} {hit.orientation}@Δ{hit.offset} floor={hit.floor:.1e}"
    return Evaluation(outcome, hit.score(test.min), detail, used_onlist=True)


#: The over-length admission (scoring._over_length_admitted_by_onlist) asks a NARROWER question than
#: the onlist support gate. The support ``min`` (e.g. v2's 0.6) means "does this read carry confident,
#: 1MM-correctable barcodes?" — the bar STARsolo's own CB correction is measured against. Admission
#: asks only "is this over-sequenced dead-zone read barcode-bearing rather than cDNA?" A cDNA (or any
#: non-barcode) read hits a barcode whitelist at its CHANCE FLOOR — ``n_entries / 4**width`` ≈ 1e-4 to
#: ~2e-3 for the 10x whitelists — with negligible variance over a 200k-read sample, so a rate a few
#: hundredfold above the floor is decisive. Seqforge matches barcodes EXACTLY (no 1MM correction), so a
#: real over-sequenced barcode read with ordinary sequencing error sits well under 0.6 yet vastly above
#: the floor (GSE126954's SRX5411291 — the perfect-whitelist fixtures hit ~1.0 and never exposed this).
#: The floor-anchored bar admits it without ever admitting a same-length cDNA read.
_OVERLENGTH_ADMISSION_MIN = 0.05  # absolute floor: a meaningful barcode signal, not chance
_OVERLENGTH_ADMISSION_FLOOR_MULT = 50.0  # and always well clear of THIS whitelist's chance floor


def onlist_admits_over_length(
    test: OnlistHitRate, read: Read, wp: WindowProbe, spec: Spec, registry: OnlistRegistry
) -> bool:
    """True iff an onlist test's window hits the whitelist far enough above chance to call the read
    barcode-bearing (not cDNA) — the floor-anchored admission bar, NOT the support ``min``.

    Mirrors :func:`_eval_onlist`'s window/whitelist resolution exactly, but decides on a lower,
    floor-derived threshold. Any unresolved / unmaterialized onlist yields ``False`` (no admission
    without a whitelist to check against), matching the gate's ABSTAIN.
    """
    ref = spec.onlists.get(test.onlist)
    if ref is None or not registry.has(ref.registry):
        return False
    try:
        packed = registry.packed(ref.registry)
    except OnlistNotAvailable:
        return False
    anchored = _anchored_element(test, read)
    if anchored is not None:
        hit = wp.anchored_onlist_hit(read, anchored.name, packed, orientation=test.orientation)
    else:
        start, _ = _window_for(test, read)
        hit = wp.onlist_hit(
            start, packed, orientation=test.orientation, offset_scan=test.offset_scan
        )
    bar = max(_OVERLENGTH_ADMISSION_MIN, hit.floor * _OVERLENGTH_ADMISSION_FLOOR_MULT)
    return hit.hit_rate >= bar


def _eval_motif(test: MotifPresent, wp: WindowProbe) -> Evaluation:
    rate = wp.motif_rate(
        test.motif,
        where=test.where,
        search_start=test.search_start,
        search_end=test.search_end,
        max_mismatch=test.max_mismatch,
    )
    if rate is None:
        return Evaluation(Outcome.ABSTAIN, 0.0, "no reads long enough")
    outcome = Outcome.PASS if rate >= test.min_rate else Outcome.FAIL
    return Evaluation(outcome, _clip(rate / max(test.min_rate, 1e-9)), f"motif_rate={rate:.2f}")


def _eval_base_composition(test: BaseComposition, read: Read, wp: WindowProbe) -> Evaluation:
    start, end = _window_for(test, read)
    frac = _mean_base_fraction(wp, start, end, test.base)
    if frac is None:
        return Evaluation(Outcome.ABSTAIN, 0.0, "window unreadable")
    outcome = Outcome.PASS if frac >= test.min_fraction else Outcome.FAIL
    return Evaluation(
        outcome, _clip(frac / max(test.min_fraction, 1e-9)), f"{test.base}={frac:.2f}"
    )


def _eval_header_index(test: HeaderIndex, wp: WindowProbe) -> Evaluation:
    grammar = wp.observation.read_name
    if grammar.sra_normalized:
        return Evaluation(Outcome.ABSTAIN, 0.0, "SRA-normalized header (index stripped)")
    has_index = grammar.index is not None
    outcome = Outcome.PASS if has_index == test.present else Outcome.FAIL
    return Evaluation(outcome, 1.0 if has_index == test.present else 0.0, f"has_index={has_index}")


def read_length_compatible(read: Read, wp: WindowProbe) -> Outcome:
    """Implicit per-role gate from the Read's declared ``min_len`` / ``max_len`` (a real requires).

    A file can fill a role only if its mode length is compatible with the read's declared geometry:
    a fixed read demands an exact mode; a variable read demands ``mode >= min_len`` (and ``<= max_len``
    when declared). An empty file fails (it cannot fill any role).
    """
    mode = wp.mode_length
    if mode == 0:
        return Outcome.FAIL
    if read.min_len is not None and mode < read.min_len:
        return Outcome.FAIL
    if read.max_len is not None and mode > read.max_len:
        return Outcome.FAIL
    return Outcome.PASS
