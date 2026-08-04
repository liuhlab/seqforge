"""The shared vocabulary for a finished pipeline's metrics — one shape, whatever tool produced them.

``seqforge report`` renders a uniform page across every **Workflow module**, so a STARsolo bundle, a
chromap fragments summary and a bulk ``Log.final.out`` all have to arrive as the same thing. That
thing is a :class:`Metric`: a value, the words a human reads it by, and a **verdict** on whether it
looks right. The verdict is domain knowledge — "valid barcodes below 0.5 means the chemistry call or
the barcode read role is wrong" is a fact about STARsolo, not a rendering choice — so it is decided
by the code that knows the tool, and the renderer only picks a colour for it.

This module is a **leaf**: it imports nothing else from ``workflows``. That is what lets ``qc.py`` and
``fragments.py`` — the modules that WRITE those artifacts — each also own the function that reads one
back, without a cycle through the registry in ``stats.py`` that dispatches to them. Bulk needs no
third writer at all: STAR's own ``Log.final.out`` has no seqforge rule behind it, so its adapter is
two of ``qc.py``'s existing functions composed. Who writes a format owns how to read it; a bundle key
and its lookup then change in one file, or fail in one file.

Nothing here reads a file. The adapters do their own loading (a gzipped JSON, a text log), and each
keeps a **pure** ``Mapping -> SampleStats`` function underneath so its metric table can be tested
against a literal dict with no filesystem in the test.

**Pipeline, not run.** The thing measured here is one execution of a composed Snakefile. A **Run** in
this project is one *sequencing* run — the ``run`` column of the units table this same page inlines —
and ``run_id`` is the identity of a (dataset, recipe) pairing. Three senses of one word is how a
reader ends up unsure which of them a field carries, so the execution sense takes the pipeline name
everywhere.

It also carries the **cross-check** vocabulary — :class:`Finding`, :class:`Alert` — for the same
reason it carries :class:`Metric`: a rule that reads a metric back and says *which decision looks
wrong* is a fact about a specific aligner, so the shapes are shared here and the rules are written
beside the module that owns the format. The renderer never learns what a valid barcode is; it is
handed alerts and draws them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from math import exp, log
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

#: How a metric reads against its own thresholds. ``none`` is not a missing value — it is a value with
#: **no defensible threshold** (an estimated cell count means nothing without knowing what was loaded),
#: and saying so is more honest than inventing a bar for it. A metric whose value could not be found is
#: simply absent from the list, never a zero.
Level = Literal["ok", "warn", "bad", "none"]

#: Which stage of the pipeline a metric speaks *about* — the answer to "what is this number for?",
#: which is a fact about the measurement and not about how a page draws it. Six, and **closed**: the
#: report renders a group as a labelled header band over its columns, so a seventh member must break
#: a test rather than quietly render a span of columns under no heading at all.
#:
#: Declared here rather than in the renderer for the same reason :data:`Level` is: the module that
#: knows what "reads in genes" measures is the one that can say it belongs to counting and not to
#: alignment, and there is deliberately **no global key -> group table** anywhere — ``reads`` is
#: emitted by two adapters under two different labels, and a lookup keyed on the string would have to
#: guess which. The group is declared at the call site, beside ``hint`` and ``headline``.
MetricGroup = Literal["input", "barcode", "alignment", "counts", "duplication", "cells"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Metric(_Frozen):
    """One number a human can act on: what it is, what it says, and whether it looks right."""

    key: str
    label: str
    #: Which band this column sits under. **No default, deliberately**: every construction path runs
    #: through the three builders below, so an omitted group is a mypy error at the call site rather
    #: than a convention someone has to remember — and the failure it prevents is a metric rendering
    #: under a heading that misdescribes it, which is a page telling a lie in a header.
    group: MetricGroup
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


# ---- the cross-check ----------------------------------------------------------------------------
#
# A metric says a number is bad. A cross-check says WHICH DECISION looks wrong — the compiler holds
# both halves (what it decided, and what came back) and this is where they join. Everything below is
# advisory by construction: there is no writer here, nothing returns an exit code, and the shapes
# carry no path to a manifest. A rule can only ever produce one of these.


#: How loudly a rule speaks. ``likely`` is "this run is probably wrong", ``possible`` is "worth a
#: look" — and the two are the whole scale, because a third grade is a grade nobody can act on
#: differently. **Declaration order is severity order**: :func:`gather_alerts` reads the rank out of
#: this literal rather than off a second hand-written table, so worst-first is written once.
Severity = Literal["likely", "possible"]

#: Whether an alert fired on every sample that LANDED, or on some of them. It is the difference
#: between "recompose this dataset" and "look at well B7", and a reader cannot draw it from a list of
#: sample ids — on a 96-well plate, 96 ids and 94 ids look the same. So it is computed and carried.
Scope = Literal["systematic", "isolated"]

#: Which decision an alert points at. Closed, and it grows **one rule at a time**: every member is
#: resolved to the value the workspace currently carries by ``report/collect.py``, and an
#: exhaustiveness test over ``get_args`` holds that shut — a member with no rule behind it and no
#: resolver in front of it would render as a field name a reader cannot act on.
#:
#: ``chemistry`` is the manifest's ``library.chemistry`` equivalence class; ``read_roles`` is which
#: file was handed over as which read (``library.files[].read_id``). Both are decisions this compiler
#: made and recorded, which is the entire premise: a bad number is only actionable once the thing that
#: produced it is named.
#:
#: ``annotation`` is the recipe's ``processing.genome`` — the registered gene model reads are counted
#: against. ``strand`` is the odd one and is deliberately kept in the same set anyway: it is **not** a
#: recipe field but a KB **backend param** (``soloStrand``), byte-decided and owned by the chemistry
#: spec, which ``compose`` emits into the config — see ``docs/adr/0011``. A reader can still act on
#: it, which is the only membership test this literal has; where they act is what the resolver's label
#: has to say, because a reader sent to edit a ``soloStrand`` in their recipe will not find one.
Decision = Literal["chemistry", "read_roles", "annotation", "strand"]

#: How each severity reads in words. Total over the literal — the same exhaustiveness shape
#: :data:`Level` and :data:`MetricGroup` already carry, so a third severity breaks a test instead of
#: rendering a badge whose word is a raw token.
SEVERITY_PHRASE: dict[Severity, str] = {
    "likely": "this run is probably wrong",
    "possible": "worth a look",
}

#: Severity -> its place in the triage order, read out of the literal's own declaration order. A
#: second hand-written table would be a second owner of "which is worse", and the one that drifts is
#: the one nothing reads at import.
_SEVERITY_RANK: dict[Severity, int] = {s: i for i, s in enumerate(get_args(Severity))}


class Finding(_Frozen):
    """One rule's verdict on ONE sample. Pure, and **not yet attributed**.

    A rule is a function over one sample's metrics, so this is everything such a function can honestly
    say: what fired, on whom, what the numbers were, and which decisions could produce them. What
    those decisions are currently *set to* is not knowable here — that needs a manifest and a recipe,
    which this module deliberately cannot see — so it is added later, by the collector, and the two
    halves meet in :func:`gather_alerts`.
    """

    #: Stable and dotted, ``<module-word>.<what-fired>``. Stable is the load-bearing word: it is what
    #: an automated consumer suppresses or tracks a recurring alert by, so it is a key and never a
    #: sentence, and it survives a reworded title.
    alert_id: str
    sample_id: str
    #: The claim, one short sentence. Identical across every sample a rule fires on — it is the
    #: rule's statement, not this sample's.
    title: str
    severity: Severity
    #: What was measured, **with this sample's values in it**. The one field that varies across a
    #: group, and the reason an alert can be judged rather than trusted.
    measured: str
    implicates: list[Decision]
    #: What a reader might change. Identical across samples, like ``title``: a remedy that varied by
    #: sample would be a diagnosis this layer is not entitled to make.
    remedy: str


class DecisionRef(_Frozen):
    """A decision an alert points at, resolved to what the workspace currently says.

    ``label`` speaks the manifest's and the recipe's own vocabulary (``chemistry (manifest
    library.chemistry)``) because the next thing a reader does is open that file and look for that
    field. ``change_to`` is filled only where the alternative is genuinely enumerable — a strand
    setting has exactly one other value, a chemistry call has a KB's worth — and stays ``None``
    otherwise rather than guessing, since a wrong concrete suggestion is worse than none.
    """

    decision: Decision
    label: str
    value: str
    change_to: str | None = None


class Alert(_Frozen):
    """A :class:`Finding` grouped over the samples it fired on, and attributed.

    ``n_samples`` is how many samples LANDED, carried beside the list that fired so the page can say
    "3 of 12" and never a bare list of ids. ``measured`` is per firing sample, in the same order as
    ``samples``: collapsing three samples to one number would throw away the evidence for the alert's
    own claim.
    """

    id: str
    title: str
    severity: Severity
    scope: Scope
    samples: list[str]
    n_samples: int
    measured: list[str]
    implicates: list[DecisionRef]
    remedy: str


def gather_alerts(
    findings: Sequence[Finding],
    *,
    n_samples: int,
    resolve: Callable[[Decision], DecisionRef | None],
) -> list[Alert]:
    """Group per-sample findings into alerts, attribute each one, and put them in one total order.

    **Pure, and shared; ``resolve`` is injected.** That is the whole seam: the grouping and the
    ordering are one implementation whatever module produced the findings, and the only thing that
    knows what a manifest is stays in ``report/collect.py``. A decision that workspace cannot answer
    for — a manifest field a stripped install cannot read — is **dropped**, not rendered as an empty
    row, because a field name with no value beside it reads as a value of nothing.

    ``n_samples`` is what LANDED (``PipelineStats.n_found``) and never what was contracted: a partial
    run must still produce alerts, and a rule that fired on both of the two samples that finished has
    fired on every sample there is to fire on.

    Two orders, and both are total. Samples inside a group sort by id, and alerts sort by severity
    then by id — a reader triages the loudest first, and an id is total because two findings sharing
    one are the same alert by construction. Sorting rather than keeping arrival order is what makes
    two renders of one workspace byte-identical however the findings reached here.

    ``title``, ``severity``, ``implicates`` and ``remedy`` are identical across a group by
    construction — one rule writes them and they do not vary by sample. Where a rule breaks that, the
    first finding in sample order wins; that is a tie-break, not a merge, and a rule whose claim
    changes per sample is a rule that wanted two ids.
    """
    grouped: dict[str, list[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.alert_id, []).append(finding)

    alerts: list[Alert] = []
    for alert_id, group in grouped.items():
        ordered = sorted(group, key=lambda f: f.sample_id)
        head = ordered[0]
        fired = {f.sample_id for f in ordered}
        refs = [resolve(d) for d in dict.fromkeys(head.implicates)]
        alerts.append(
            Alert(
                id=alert_id,
                title=head.title,
                severity=head.severity,
                scope="systematic" if n_samples > 0 and len(fired) >= n_samples else "isolated",
                samples=[f.sample_id for f in ordered],
                n_samples=n_samples,
                measured=[f.measured for f in ordered],
                implicates=[r for r in refs if r is not None],
                remedy=head.remedy,
            )
        )
    return sorted(alerts, key=lambda a: (_SEVERITY_RANK[a.severity], a.id))


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
    #: What the module's cross-checks said about the samples above — **here and not on**
    #: :class:`SampleStats`. That carries what the artifact SAID; a finding is a judgement about a
    #: decision, and R4 is that one judgement is one envelope. It also makes the scope question
    #: answerable: "did this fire on every sample" is a fact about the pipeline, not about a sample.
    findings: list[Finding] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Every contracted sample landed and parsed — the only state the page tints green.

        ``n_expected > 0`` is load-bearing: a config carrying no sample list at all would otherwise
        satisfy ``0 == 0`` and report a pipeline that produced nothing as one that finished
        everything. Vacuous truth is the wrong answer for a badge whose whole job is saying whether
        the work is done.
        """
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
    group: MetricGroup,
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
        group=group,
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
    group: MetricGroup,
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
        group=group,
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
    group: MetricGroup,
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
        group=group,
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
    "Alert",
    "Decision",
    "DecisionRef",
    "Finding",
    "Level",
    "Metric",
    "MetricGroup",
    "SEVERITY_PHRASE",
    "SampleStats",
    "Scope",
    "Severity",
    "PipelineStats",
    "MAX_KNEE_POINTS",
    "count",
    "gather_alerts",
    "fmt_count",
    "fmt_int",
    "fmt_pct",
    "fmt_ratio",
    "fraction",
    "grade",
    "knee_points",
    "ratio",
]
