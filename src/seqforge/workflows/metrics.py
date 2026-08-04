"""The shared vocabulary for a finished pipeline's metrics — one shape, whatever tool produced them.

``seqforge report`` renders a uniform page across every **Workflow module**, so a STARsolo bundle, a
chromap fragments summary and a bulk ``Log.final.out`` all have to arrive as the same thing. That
thing is a :class:`Metric`: a value, the words a human reads it by, and a **verdict** on whether it
looks right. The verdict is domain knowledge — "valid barcodes below 0.5 means the chemistry call or
the barcode read role is wrong" is a fact about STARsolo, not a rendering choice — so it is decided
by the code that knows the tool, and the renderer only picks a colour for it.

This module is a **leaf**: it imports nothing else from ``workflows``. That is what lets ``qc.py`` —
the module that WRITES the artifact — also own the function that reads it, without a cycle through
the registry in ``stats.py`` that dispatches to it, and the same will hold for ``fragments.py`` and
for bulk STAR when their adapters land. Who writes a format owns how to read it; a bundle key and its
lookup then change in one file, or fail in one file.

Nothing here reads a file. The adapters do their own loading (a gzipped JSON, a text log), and each
keeps a **pure** ``Mapping -> SampleStats`` function underneath so its metric table can be tested
against a literal dict with no filesystem in the test.

**Pipeline, not run.** The thing measured here is one execution of a composed Snakefile. A **Run** in
this project is one *sequencing* run — the ``run`` column of the units table this same page inlines —
and ``run_id`` is the identity of a (dataset, recipe) pairing. Three senses of one word is how a
reader ends up unsure which of them a field carries, so the execution sense takes the pipeline name
everywhere.
"""

from __future__ import annotations

from collections.abc import Sequence
from math import exp, log
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: How a metric reads against its own thresholds. ``none`` is not a missing value — it is a value with
#: **no defensible threshold** (an estimated cell count means nothing without knowing what was loaded),
#: and saying so is more honest than inventing a bar for it. A metric whose value could not be found is
#: simply absent from the list, never a zero.
Level = Literal["ok", "warn", "bad", "none"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Metric(_Frozen):
    """One number a human can act on: what it is, what it says, and whether it looks right."""

    key: str
    label: str
    #: The raw number, kept beside the formatted string so a machine consumer (the JSON summary, a
    #: future corpus-level aggregate) never has to parse ``"97.8%"`` back into a float.
    value: float
    #: The same number formatted for a cell — units and precision decided by the code that knows what
    #: the number IS, so the renderer never guesses whether 0.978 is a fraction or a count.
    display: str
    level: Level = "none"
    #: One sentence: what this measures, and what a bad value would mean. Shown on hover/expand, so a
    #: reader who has never run STARsolo can still tell whether the pipeline looks correct.
    hint: str = ""
    #: True for the few that belong in the at-a-glance strip; the rest live in the full table.
    headline: bool = False


class SampleStats(_Frozen):
    """One sample's finished output: its metrics, and the knee plot when the tool produced one."""

    sample_id: str
    metrics: list[Metric] = Field(default_factory=list)
    #: ``(rank, value)`` pairs, log-spaced — see :func:`knee_points`. Empty when the tool writes no
    #: per-barcode vector (bulk STAR has no cells; chromap's fragments summary keeps no vector).
    knee: list[tuple[int, int]] = Field(default_factory=list)
    #: Context the adapter wants carried up — e.g. which ``soloFeatures`` feature these came from.
    note: str = ""


class PipelineStats(_Frozen):
    """Every finished sample of one compiled pipeline, plus an honest account of what is missing.

    ``n_expected`` comes from the composed ``config.yaml``'s own ``samples`` list — the same artifact
    the pipeline consumed — so "did it finish" is answered by the files it was contracted to produce,
    not by parsing a snakemake log and not by listing the results tree. A listing can say what landed
    and can never say what is missing, so a partial pipeline read that way is indistinguishable from a
    complete one. This renders what landed and says how much did.
    """

    module: str
    n_expected: int
    n_found: int
    samples: list[SampleStats] = Field(default_factory=list)
    #: ``(key, label)`` in display order — the General-Statistics column set for this module. A union
    #: across samples, so a sample missing one metric leaves a gap rather than dropping the column.
    columns: list[tuple[str, str]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.n_expected > 0 and self.n_found == self.n_expected


# ---- formatting ---------------------------------------------------------------------------------


def fmt_pct(value: float) -> str:
    """A 0–1 fraction as a percentage. STARsolo reports rates as fractions; humans read percents."""
    return f"{value * 100:.1f}%"


def fmt_count(value: float) -> str:
    """A large count, abbreviated. 207946411 -> ``207.9M``; small numbers keep their digits."""
    n = float(value)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= limit:
            return f"{n / limit:.1f}{suffix}"
    return f"{n:,.0f}"


def fmt_int(value: float) -> str:
    """An exact integer with thousands separators — for counts small enough to read in full."""
    return f"{value:,.0f}"


def fmt_ratio(value: float) -> str:
    """A ratio or a mean, to one decimal.

    Its own formatter because rounding a *graded* ratio to an integer is a lie the colour then
    contradicts: at a bar of 4.0, 3.9 and 4.1 both render ``"4"`` and sit in the table one amber and
    one red, which reads as a rendering bug rather than as a threshold. A count can round — it is
    already whole. A ratio cannot.
    """
    return f"{value:,.1f}"


# ---- grading ------------------------------------------------------------------------------------


def grade(
    value: float, *, ok: float | None, warn: float | None, higher_is_better: bool = True
) -> Level:
    """Two thresholds -> a :data:`Level`. ``ok=None`` means "no defensible bar" and yields ``none``.

    Deliberately two-sided via ``higher_is_better`` rather than two functions: "% of reads unmapped:
    too short" is graded the same way as "% uniquely mapped", just with the comparison flipped, and one
    function means one place where the boundary condition (``>=`` vs ``>``) is decided.
    """
    if ok is None or warn is None:
        return "none"
    if higher_is_better:
        return "ok" if value >= ok else "warn" if value >= warn else "bad"
    return "ok" if value <= ok else "warn" if value <= warn else "bad"


def fraction(
    key: str,
    label: str,
    value: float | None,
    *,
    ok: float | None = None,
    warn: float | None = None,
    higher_is_better: bool = True,
    hint: str = "",
    headline: bool = False,
) -> Metric | None:
    """A 0–1 rate as a graded, percent-formatted :class:`Metric`. ``None`` in, ``None`` out.

    Returning ``None`` for an absent value is what keeps a missing key out of the page entirely. The
    alternative — a zero, or a dash rendered as if it were data — is how a reader ends up trusting a
    number the tool never wrote.
    """
    if value is None:
        return None
    return Metric(
        key=key,
        label=label,
        value=float(value),
        display=fmt_pct(float(value)),
        level=grade(float(value), ok=ok, warn=warn, higher_is_better=higher_is_better),
        hint=hint,
        headline=headline,
    )


def count(
    key: str,
    label: str,
    value: float | None,
    *,
    ok: float | None = None,
    warn: float | None = None,
    higher_is_better: bool = True,
    exact: bool = False,
    hint: str = "",
    headline: bool = False,
) -> Metric | None:
    """A count as a graded :class:`Metric`. ``exact`` keeps every digit instead of abbreviating."""
    if value is None:
        return None
    n = float(value)
    return Metric(
        key=key,
        label=label,
        value=n,
        display=fmt_int(n) if exact else fmt_count(n),
        level=grade(n, ok=ok, warn=warn, higher_is_better=higher_is_better),
        hint=hint,
        headline=headline,
    )


def ratio(
    key: str,
    label: str,
    value: float | None,
    *,
    ok: float | None = None,
    warn: float | None = None,
    higher_is_better: bool = True,
    hint: str = "",
    headline: bool = False,
) -> Metric | None:
    """A derived ratio or mean as a graded :class:`Metric`, rendered to one decimal.

    The third builder because a derived number is neither of the other two: it is not a 0–1 rate
    (:func:`fraction` would render 3.9 as ``390.0%``) and it is not a whole count
    (:func:`count` would round it past its own threshold — see :func:`fmt_ratio`).
    """
    if value is None:
        return None
    n = float(value)
    return Metric(
        key=key,
        label=label,
        value=n,
        display=fmt_ratio(n),
        level=grade(n, ok=ok, warn=warn, higher_is_better=higher_is_better),
        hint=hint,
        headline=headline,
    )


# ---- the knee vector ----------------------------------------------------------------------------

#: How many points of a knee vector reach the page. STAR writes one integer per barcode — on a 10x v3
#: whitelist that is ~6.8 million lines, and the whole report is one self-contained HTML with a 500 KB
#: budget. 200 points draw the same curve at a few KB.
MAX_KNEE_POINTS = 200


def knee_points(
    vector: Sequence[int], *, max_points: int = MAX_KNEE_POINTS
) -> list[tuple[int, int]]:
    """A descending per-barcode vector -> ``(rank, value)`` pairs, **log-spaced** in rank.

    Log-spaced and not uniform, because a knee plot is read on log axes: uniform sampling spends
    almost every point on the flat tail and renders the knee itself — the one feature anybody looks at
    — as two pixels. The first and last ranks are always kept, so the curve's extent is exact.
    """
    n = len(vector)
    if n == 0:
        return []
    if n <= max_points:
        return [(i + 1, int(v)) for i, v in enumerate(vector)]
    ranks = sorted(
        {max(1, min(n, round(exp(log(n) * i / (max_points - 1))))) for i in range(max_points)}
    )
    return [(r, int(vector[r - 1])) for r in ranks]


__all__ = [
    "Level",
    "Metric",
    "SampleStats",
    "PipelineStats",
    "MAX_KNEE_POINTS",
    "count",
    "fmt_count",
    "fmt_int",
    "fmt_pct",
    "fmt_ratio",
    "fraction",
    "grade",
    "knee_points",
    "ratio",
]
