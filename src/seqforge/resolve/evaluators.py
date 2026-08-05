"""Signature-test evaluators — the CLOSED set that mirrors ``kb.schema`` exactly.

``evaluate(test, read, wp, spec, registry)`` returns an :class:`Evaluation` carrying both a gate
``outcome`` (``PASS`` / ``FAIL`` / ``ABSTAIN``) and a supports ``score`` in ``[0, 1]``. The caller
uses ``outcome`` for ``requires`` / ``excludes`` gates and ``score`` for ``supports`` weighting.

Two invariants hold regardless of where a test is placed:
- **``ABSTAIN`` never gates.** "The probe cannot see this signal" is not "the signal is absent" — an
  abstaining requires/excludes test is a pass-through, not a rejection (an SRA-normalized
  ``header_index`` must not reject every SRA dataset).
- **``distinct_ratio`` never gates.** It is depth-dependent: its gate outcome is forced to ``ABSTAIN``
  so a misplaced ``requires`` cannot use it; its supports ``score`` remains meaningful.

Those two invariants are why **``ABSTAIN`` cannot answer "could the bytes answer this"**, and why
``answerable`` is a separate field rather than a property of the outcome. The second invariant makes
``distinct_ratio`` abstain on every input while measuring on every input, so a caller reading the
outcome alone cannot tell it from an onlist test that had no whitelist to check against. ``supports``
weighting needs exactly that distinction — a test the bytes were silent about must leave the
normalizer instead of scoring its spec down (#307) — so every "the bytes are silent" return in this
file is built by :func:`_unanswerable`, every "we had no list" return by :func:`_unconfirmed`, and
nothing else builds either.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

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
    Test,
)
from ..models.observation import CycleComposition
from .window import WindowProbe

#: ``has_segment kind: constant`` is a floor on the SHARE OF READS carrying the window's sequence, not
#: on a per-cycle purity averaged over the whole head. The mean cannot tell "every read carries this
#: linker" from "most do and the rest of the head is junk", and those two only coincide on a head with
#: no junk in it — which is the one kind of head a generated fixture ever has. Calibrated there, a 0.9
#: purity bar forbade real SPLiT-seq's barcode read (linker1 0.905, linker2 0.827, and 0.99+ over the
#: ~61% of reads that are genuinely SPLiT-seq), so the generic paired-end fallback won on geometry at
#: exit 0 and three correct whitelists were never consulted.
#:
#: A majority is the bar because that is what "the reads carry this sequence" means about a
#: population. Junk reads are COUNTED, never filtered: they stay in the denominator, so contamination
#: lowers the statistic instead of being removed from its own measurement. Real SPLiT-seq measures
#: 0.85 (linker1) and 0.73 (linker2) here, against ~0 for any window that has no fixed sequence in it.
#:
#: THE ONLIST HIT RATE TAKES THE OPPOSITE POLICY, and the two do not contradict each other (#177). A
#: read that does not carry the linker is a real read that genuinely is not this chemistry — evidence
#: AGAINST it, so filtering it would remove contamination from the measurement of contamination. A read
#: whose barcode cycle was never called is evidence for nothing at all: it cannot hit any whitelist, so
#: counting it measures the run's base-calling instead of the library, which is why `io.onlist` drops it
#: from that denominator and reports the loss as coverage. The rule that produces both answers is the
#: same one: keep what the LIBRARY did, drop what only the RUN did.
_CONSTANT_CARRIER_MIN = 0.5
#: Slack to the modal consensus, per base of window: 3 of 30 for a SPLiT-seq linker, 1 of 12 for a BD
#: Rhapsody one. It absorbs sequencing error and nothing else — the statistic is nearly flat in it
#: (real linker2 moves 0.719 -> 0.732 from one mismatch to three), while the chance of a random read
#: clearing it stays vanishing.
_CONSTANT_MISMATCH_ALLOWANCE = 0.1
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
    #: Could THESE BYTES have answered this test? False means they were silent — no read reaching the
    #: column, a header the archive stripped — so the ``score`` of 0.0 is a placeholder and not a
    #: measurement, and ``scoring`` drops the test from the support normalizer rather than marking the
    #: spec down for a question nobody could have answered (#307).
    #:
    #: **A missing whitelist is answerable and stays True.** The bytes were willing; we lacked the
    #: list. A rival spec whose list did materialize answered the same question and pays for every
    #: imperfection in its hit rate, so renormalizing around this one would make the unverifiable spec
    #: the cheaper to satisfy — see :func:`_unconfirmed`.
    #:
    #: **It is its own field because ``outcome`` cannot answer this.** ABSTAIN is overloaded: it is
    #: what "the probe could not see" returns, AND what ``distinct_ratio`` returns on every input by
    #: design, so a depth-dependent statistic can never be written into a ``requires`` and gate a spec
    #: away. Reading answerability off the outcome would discard every ``distinct_ratio`` measurement
    #: in the KB — on 10x's barcode read that is the only evidence left once the onlist is withheld,
    #: and the chemistry would score 0.0 on the reads it generated.
    answerable: bool = True


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _unanswerable(detail: str) -> Evaluation:
    """The DATA could not answer this test — it gates nothing and it normalizes nothing.

    Every "the bytes are silent here" return in this file goes through here, so the 0.0 that stands in
    for the unmeasured score can never be mistaken for a measured one by a caller that reads ``score``
    without ``answerable``. The one ABSTAIN that is NOT built here is ``distinct_ratio``'s scored
    return, and that exception is the whole reason answerability is not read off the outcome.

    Deliberately NOT called "inapplicable": that word is already taken, by the read-set rule a test
    addressed to a read outside the active set falls under (:func:`~seqforge.resolve.scoring
    ._evaluate_read_set`, ADR-0029). Both end in "leaves the numerator and its normalizer", and a
    reader who has to work out which of the two a sentence means is one term short.
    """
    return Evaluation(Outcome.ABSTAIN, 0.0, detail, answerable=False)


def _unconfirmed(detail: str) -> Evaluation:
    """WE could not ask — the whitelist was missing, so the test stands unconfirmed rather than unread.

    The other half of #307, and the half that decides whether the fix is safe. A support leaves the
    normalizer when the BYTES could not answer it, because then no chemistry could have got an answer
    there and dropping it advantages nobody. An onlist we could not obtain is not that: the bytes were
    willing, and for a RIVAL spec whose list did materialize the same question WAS answered. Drop it
    from the normalizer and the rival pays for every imperfection in its hit rate while this spec pays
    nothing, so the unverifiable spec becomes the cheaper one to satisfy. The weight stays: a spec is
    not credited for evidence nobody was able to check.

    That leaves the case #307 measured — the onlist withheld from EVERY spec at once — and it is not
    this one. There the question is asked of nobody, so it leaves the SIGNATURE rather than the
    normalizer; :func:`~seqforge.resolve.confuse.without_rung3_evidence` is where that happens.

    Both directions are measured in `docs/research/support-normalizer-asymmetry.md` (2026-08-05).
    """
    return Evaluation(Outcome.ABSTAIN, 0.0, detail, answerable=True)


def _window_for(test: object, read: Read) -> tuple[int, int | None]:
    """Resolve a test's FIXED target window from ``element`` name XOR explicit ``(start, end)``.

    Only valid for fixed-offset elements. A **floating** element has no constant window — its per-read
    frame is resolved by :meth:`WindowProbe._frames`, and the callers below route to the ``anchored_*``
    methods via :func:`_anchored_element` before ever reaching here.
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
    test: Test, read: Read, wp: WindowProbe, spec: Spec, registry: OnlistRegistry
) -> Evaluation:
    """Evaluate one signature test against a file's :class:`WindowProbe`.

    ``test`` is the KB's ``Test`` union rather than ``object``, and the dispatch ends in
    ``assert_never`` rather than in a default: the union is the DSL's whole vocabulary, so a member
    with no branch here is a spec key the scorer silently ignores. The default was
    ``Evaluation(ABSTAIN, 0.0, "not a per-cell test")``, and it is what let ``read_count`` read as a
    gate in all 16 shipped signatures while gating nothing, with nothing anywhere going red (#276).
    Adding a word to the DSL is now a type error here until the scorer is given a meaning for it —
    the shape ``compose/core.py``'s ``_read_files_in`` takes, for the same reason.
    """
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
    assert_never(test)


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
    if test.kind == "constant":
        if end is None:
            # An open-ended element has no fixed column to be constant over, and a window that runs to
            # whichever read is longest is not one either. "Cannot see it" is not "it is absent".
            return _unanswerable("open-ended window")
        tolerance = int((end - start) * _CONSTANT_MISMATCH_ALLOWANCE)
        rate = wp.consensus_match_rate(start, end, tolerance)
        if rate is None:
            # No read reaches this column, so nothing about it was observed. The mean this replaced
            # answered anyway, off however many cycles the short reads did cover, and called a
            # 10-cycle prefix of a 30 bp linker a verdict on the linker. Whether such a file can fill
            # the role is the declared geometry's question, and `read_length_compatible` asks it.
            return _unanswerable("no read reaches this column")
        outcome = Outcome.PASS if rate >= _CONSTANT_CARRIER_MIN else Outcome.FAIL
        return Evaluation(
            outcome,
            _clip(rate / _CONSTANT_CARRIER_MIN),
            f"consensus_rate={rate:.2f} min={_CONSTANT_CARRIER_MIN} mismatch<={tolerance}",
        )
    if test.kind == "random":
        # Near-uniformity IS a population property, so this one stays a mean over cycles.
        mmf = _mean_max_fraction(wp, start, end)
        if mmf is None:
            return _unanswerable("window unreadable")
        outcome = Outcome.PASS if mmf <= _RANDOM_MAXFRAC else Outcome.FAIL
        return Evaluation(outcome, _clip((_RANDOM_MAXFRAC - mmf) / 0.3), f"mean_maxfrac={mmf:.2f}")
    base = "T" if test.kind == "polyT" else "A"
    frac = _mean_base_fraction(wp, start, end, base)
    if frac is None:
        return _unanswerable("window unreadable")
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
            return _unanswerable("open-ended window")
        ratio = wp.distinct_ratio(start, end)
        detail = ""
    if ratio is None:
        return _unanswerable("window unreadable")
    score = _clip(1.0 - ratio) if test.expect == "low" else _clip(ratio)
    return Evaluation(
        Outcome.ABSTAIN, score, f"{detail}distinct_ratio={ratio:.3f} expect={test.expect}"
    )


def _eval_onlist(
    test: OnlistHitRate, read: Read, wp: WindowProbe, spec: Spec, registry: OnlistRegistry
) -> Evaluation:
    ref = spec.onlists.get(test.onlist)
    if ref is None:
        return _unconfirmed(f"unknown onlist alias {test.onlist!r}")
    if not registry.has(ref.registry):
        return _unconfirmed(f"onlist {ref.registry!r} not registered")
    try:
        packed = registry.packed(ref.registry)
    except OnlistNotAvailable:
        return _unconfirmed(f"onlist {ref.registry!r} not materialized")
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
        # Two causes, one verdict: no read reached the motif's width, or none was called where the
        # motif was looked for. Naming only the first was a lie a dark cycle told — and either way
        # the probe could not see, which never gates.
        return _unanswerable("no read could carry the motif (short or uncalled)")
    outcome = Outcome.PASS if rate >= test.min_rate else Outcome.FAIL
    return Evaluation(outcome, _clip(rate / max(test.min_rate, 1e-9)), f"motif_rate={rate:.2f}")


def _eval_base_composition(test: BaseComposition, read: Read, wp: WindowProbe) -> Evaluation:
    start, end = _window_for(test, read)
    frac = _mean_base_fraction(wp, start, end, test.base)
    if frac is None:
        return _unanswerable("window unreadable")
    outcome = Outcome.PASS if frac >= test.min_fraction else Outcome.FAIL
    return Evaluation(
        outcome, _clip(frac / max(test.min_fraction, 1e-9)), f"{test.base}={frac:.2f}"
    )


def _eval_header_index(test: HeaderIndex, wp: WindowProbe) -> Evaluation:
    grammar = wp.observation.read_name
    if grammar.sra_normalized:
        return _unanswerable("SRA-normalized header (index stripped)")
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
