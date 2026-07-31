"""Render one ``seqforge eval run`` report as a single self-contained HTML page.

The input is the JSON ``eval run`` already writes to stdout — this module is a *consumer* of that
stream, never a second output mode for it ([ADR-0013](../../../docs/adr/0013-cli-is-a-machine-interface.md)
makes machine JSON the contract and forbids a ``--json`` switch). Same shape as
:mod:`seqforge.report`: every asset is a real file under ``assets/``, read via ``importlib.resources``
and inlined, and the page makes zero network requests — a CI artifact is downloaded and opened from
``file://``, where an external stylesheet renders nothing.

The page's *shell* is ``assets/eval-report.html``: the doctype, the head, the header chrome and the
order of the sections, with ``{{SLOT}}`` markers this module fills. That keeps the layout editable
without reading Python string concatenation, and it is deliberately not a templating engine —
:func:`_fill` is one regex pass with no expressions, no loops and no dependency, so the template can
never contain logic and the fragments below stay the only place a decision is made about what to show.

What the page is *for*, in the order it is laid out:

**A false accept is a verdict, not a percentage.** ``eval run`` exits 3 on any false accept and
deliberately not on a ``--fail-under`` slider, because no threshold makes one tolerable. So the banner
under the title states it in words and names the cases, and the tile counts them rather than rating
them. Folding that into an accuracy figure is precisely the lie the eval corpus exists to prevent.

**Worst grade first, and severity is carried in form.** Cases sort by :data:`GRADE_ORDER`, each carries
a severity stripe and a pill, and a false accept gets a filled pill where every other grade gets an
outline — so the one failure that poisons the corpus does not look like the four that cost attention.
Correct cases collapse; failures open.

**A skip is a state, not an omission.** An unreachable benchmark package is a real outcome of the HF
tier. Skips render with their reason on the face of the card and are excluded from every rate, exactly
as ``build_report`` excludes them — a skip is never a pass.

**Both times, labelled.** ``cost.seconds`` is the sum of the per-case durations (work done);
``cost.wall_seconds`` is the elapsed time. Under the parallel runner they differ by the fan-out, and
calling the sum "wall time" — as this renderer's predecessor did — reports a run as ten times slower
than it was.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from html import escape
from importlib.resources import files
from typing import Any

#: Bumped when the page's layout or projection changes. Not folded into any content-addressed key —
#: the report is a rebuildable view of a JSON file, never an input to anything.
EVAL_REPORT_VERSION = "2026.7.1"

#: The grade vocabulary, worst first. ``false_accept`` leads because it is the one failure the corpus
#: never recovers from: a confident wrong manifest that nobody looks at again (``evals/README.md``).
GRADE_ORDER: list[str] = [
    "false_accept",
    "mis_triage",
    "false_refuse",
    "wrong_reason",
    "over_ask",
    "correct",
]

#: What each grade means, and what it costs — the two columns of ``evals/README.md``'s table. The cost
#: is on the page because the ordering above is otherwise just an assertion.
GRADE_BLURB: dict[str, tuple[str, str]] = {
    "false_accept": (
        "decided wrong, or decided at all when it should have stopped",
        "a human never looks again; the corpus is silently poisoned",
    ),
    "mis_triage": (
        "refused when it should have asked, or vice versa",
        "stopped, but sends the human the wrong way",
    ),
    "false_refuse": (
        "blocked on something it should have decided or asked",
        "throughput — a human looks and unblocks it",
    ),
    "wrong_reason": (
        "right outcome, wrong blocker code or conflict",
        "the refusal's meaning has rotted",
    ),
    "over_ask": ("asked what code could settle", "a question that did not need asking"),
    "correct": ("as expected", "none"),
}

#: Grade -> severity level. The levels are what the CSS colours, so a grade added to the vocabulary
#: without a level here renders neutral rather than mis-coloured. ``false_accept`` gets a level of its
#: own on purpose: it is not a worse shade of "bad", it is the failure with no tolerable rate, and the
#: page must not let it read as one more red row.
GRADE_LEVEL: dict[str, str] = {
    "false_accept": "poison",
    "mis_triage": "bad",
    "false_refuse": "bad",
    "wrong_reason": "warn",
    "over_ask": "warn",
    "correct": "ok",
}

#: Level -> sort rank, so an unknown grade sorts just above a skip rather than silently first or last.
_UNKNOWN_RANK = len(GRADE_ORDER)
_SKIP_RANK = _UNKNOWN_RANK + 1


def esc(value: Any) -> str:
    """Escape for HTML text. Every value here is archive prose, a case id or a shell command."""
    return escape("" if value is None else str(value), quote=True)


def _asset(name: str) -> str:
    """Read a packaged asset (``evals/assets/<name>``) as text."""
    return (files(__package__) / "assets" / name).read_text(encoding="utf-8")


#: A slot in the page template: ``{{NAME}}``, uppercase, nothing else.
_SLOT = re.compile(r"\{\{([A-Z_]+)\}\}")


def _fill(template: str, **slots: str) -> str:
    """Substitute every ``{{SLOT}}`` in the page template, in ONE pass.

    Not ``str.format``: the template is HTML that will grow CSS-ish or JS-ish braces sooner or later,
    and a format string turns each of those into a crash or a silent hole. Not repeated ``.replace``
    either — one pass means a slot's *content* can contain ``{{X}}`` (a report renders arbitrary
    archive prose, and a case id is not ours) without being substituted a second time.

    A slot the caller did not supply raises rather than rendering ``{{TILES}}`` as page text, and a
    slot the caller supplied that the template does not have raises too: both are edits to one of the
    two files that forgot the other.
    """
    seen: set[str] = set()

    def one(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in slots:
            raise KeyError(f"eval-report.html asks for a slot the renderer does not fill: {name}")
        seen.add(name)
        return slots[name]

    out = _SLOT.sub(one, template)
    unused = sorted(set(slots) - seen)
    if unused:
        raise KeyError(f"the renderer fills slots eval-report.html does not have: {unused}")
    return out


def _script_guard(text: str) -> str:
    """Neutralise any ``</script`` in embedded JS so it cannot close the inlining ``<script>`` early.

    ``<\\/script`` is byte-equivalent to ``</script`` in every JS string/regex context, so this never
    changes behaviour; it only guarantees the browser's tokenizer keeps reading.
    """
    return text.replace("</script", "<\\/script")


# --------------------------------------------------------------------------------------------------
# the projection: raw JSON -> something typed, so the fragments below read as HTML and not as dict
# archaeology, and so mypy checks the page against the shape `build_report` actually emits.
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldView:
    """One ``expected`` vs ``actual`` check on one dotted path."""

    path: str
    expected: Any
    actual: Any
    ok: bool


@dataclass(frozen=True)
class HarvestView:
    """How the LLM stage did on one case. Absent under ``--no-llm``, where nothing harvested."""

    matched: list[str]
    missing: list[str]
    hallucinated: list[str]
    unstable: list[str]
    n_rejected: int
    extracted: dict[str, str]

    @property
    def worth_showing(self) -> bool:
        return bool(
            self.matched or self.missing or self.hallucinated or self.unstable or self.n_rejected
        )


@dataclass(frozen=True)
class CaseView:
    """One row of ``per_case``. ``skipped`` set means every graded field is absent, not zero."""

    case_id: str
    grade: str | None
    skipped: str | None
    expected: str | None
    actual: str | None
    fields: list[FieldView]
    notes: list[str]
    missed_question: bool
    trials: int
    stability: float
    harvest: HarvestView | None
    usage: dict[str, int]
    seconds: float
    llm_calls: int

    @property
    def level(self) -> str:
        if self.skipped is not None:
            return "skip"
        return GRADE_LEVEL.get(self.grade or "", "warn")

    @property
    def rank(self) -> int:
        """Worst first; skips last. A skip is not a grade, so it does not compete for the top."""
        if self.skipped is not None:
            return _SKIP_RANK
        try:
            return GRADE_ORDER.index(self.grade or "")
        except ValueError:
            return _UNKNOWN_RANK

    @property
    def bad_fields(self) -> list[FieldView]:
        return [f for f in self.fields if not f.ok]

    @property
    def unstable_across_trials(self) -> bool:
        """Correct only some of the time. A stage right 2 times in 3 is not right (``run.py``)."""
        return self.trials > 1 and self.stability < 1.0


def _case_view(raw: dict[str, Any]) -> CaseView:
    harvest = raw.get("harvest")
    return CaseView(
        case_id=str(raw.get("case", "?")),
        grade=raw.get("grade"),
        skipped=raw.get("skipped"),
        expected=raw.get("expected"),
        actual=raw.get("actual"),
        fields=[
            FieldView(
                path=str(f.get("path", "?")),
                expected=f.get("expected"),
                actual=f.get("actual"),
                ok=bool(f.get("ok")),
            )
            for f in raw.get("fields", [])
        ],
        notes=[str(n) for n in raw.get("notes", [])],
        missed_question=bool(raw.get("missed_question")),
        trials=int(raw.get("trials", 1) or 1),
        stability=float(raw.get("stability", 1.0) or 0.0),
        harvest=None
        if harvest is None
        else HarvestView(
            matched=[str(x) for x in harvest.get("matched", [])],
            missing=[str(x) for x in harvest.get("missing", [])],
            hallucinated=[str(x) for x in harvest.get("hallucinated", [])],
            unstable=[str(x) for x in harvest.get("unstable", [])],
            n_rejected=int(harvest.get("n_rejected", 0) or 0),
            extracted={str(k): str(v) for k, v in harvest.get("extracted", {}).items()},
        ),
        seconds=float(raw.get("seconds", 0.0) or 0.0),
        llm_calls=int(raw.get("llm_calls", 0) or 0),
        usage={str(k): int(v) for k, v in raw.get("usage", {}).items()},
    )


# --------------------------------------------------------------------------------------------------
# value formatting
# --------------------------------------------------------------------------------------------------


def _count(value: float) -> str:
    return f"{value:,.0f}"


def _seconds(value: float) -> str:
    if value >= 3600:
        return f"{value / 3600:.1f}h"
    if value >= 120:
        return f"{value / 60:.1f}m"
    return f"{value:.1f}s"


def _value_html(value: Any, other: Any) -> str:
    """One side of a field check.

    A list value is the ``experiment.samples.*.<attr>`` multiset, and the shape it really takes is
    seven samples with two distinct strains between them. So identical elements collapse to one chip
    carrying its multiplicity, and a chip is marked when its count differs from the other side's.
    Printing the raw list would show seven chips of which one differs by position — which is not the
    question a reader has. "``N2`` ×3 here, ×2 there" is.
    """
    if isinstance(value, list):
        mine: Counter[str] = Counter(str(v) for v in value)
        theirs: Counter[str] = (
            Counter(str(v) for v in other) if isinstance(other, list) else Counter()
        )
        chips = []
        for text in sorted(mine):
            differs = theirs[text] != mine[text]
            cls = "chip chip-diff" if differs else "chip"
            mult = f'<span class="mult">×{mine[text]}</span>' if mine[text] > 1 else ""
            chips.append(f'<code class="{cls}">{esc(text)}{mult}</code>')
        count = f'<span class="chip-count">{len(value)} values</span>'
        return f'<div class="chips">{count}{"".join(chips)}</div>'
    if value is None:
        return '<span class="nil">null</span>'
    if value == "":
        return '<span class="nil">empty</span>'
    return f'<code class="val">{esc(value)}</code>'


def _chips(items: list[str], cls: str = "chip") -> str:
    chips = "".join(f'<code class="{cls}">{esc(i)}</code>' for i in items)
    return f'<div class="chips">{chips}</div>'


# --------------------------------------------------------------------------------------------------
# fragments
# --------------------------------------------------------------------------------------------------


def _tile(label: str, value: str, sub: str, level: str = "none") -> str:
    return (
        f'<div class="tile lv-{esc(level)}">'
        f'<div class="tile-k">{esc(label)}</div>'
        f'<div class="tile-v tabular-nums">{value}</div>'
        f'<div class="tile-s">{sub}</div>'
        "</div>"
    )


def _banner(false_accepts: list[CaseView]) -> str:
    """The verdict, in words. Never a percentage — see this module's docstring."""
    why = (
        "A refusal costs attention; a false accept costs the corpus. "
        "<code class='val'>seqforge eval run</code> exits <b>3</b> on <b>any</b> false accept, "
        "deliberately not on a <code class='val'>--fail-under</code> threshold, because no rate "
        "makes one tolerable."
    )
    if not false_accepts:
        return (
            '<div class="banner lv-ok"><div class="banner-mark">✓</div><div class="min-w-0">'
            "<div class='banner-head'>No false accepts.</div>"
            f'<p class="banner-why">{why}</p></div></div>'
        )
    names = "".join(f'<code class="chip chip-diff">{esc(c.case_id)}</code>' for c in false_accepts)
    plural = "case" if len(false_accepts) == 1 else "cases"
    return (
        '<div class="banner lv-poison"><div class="banner-mark">!</div><div class="min-w-0">'
        f"<div class='banner-head'>FALSE ACCEPT in {len(false_accepts)} {plural}.</div>"
        f'<div class="chips mt-2">{names}</div>'
        f'<p class="banner-why">{why}</p></div></div>'
    )


def _field_table(fields: list[FieldView]) -> str:
    """Expected beside actual, per dotted path. Failures first — the same rule as the case list."""
    if not fields:
        return ""
    ordered = sorted(fields, key=lambda f: (f.ok, f.path))
    bad_row = '<tr class="row-bad">'
    rows = "".join(
        ("<tr>" if f.ok else bad_row)
        + f'<td class="align-top"><code class="path">{esc(f.path)}</code></td>'
        + f'<td class="align-top">{_value_html(f.expected, f.actual)}</td>'
        + f'<td class="align-top">{_value_html(f.actual, f.expected)}</td>'
        + f'<td class="align-top text-center">{"·" if f.ok else "✕"}</td>'
        + "</tr>"
        for f in ordered
    )
    return (
        '<div class="scroll-x mt-3"><table class="w-full text-sm">'
        '<thead><tr><th class="text-left">field</th><th class="text-left">expected</th>'
        '<th class="text-left">actual</th><th class="w-8"></th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def _harvest_block(h: HarvestView) -> str:
    """The LLM stage's own scorecard. ``hallucinated`` is corpus poison; ``n_rejected`` is the net."""
    parts = []
    if h.hallucinated:
        parts.append(
            '<div class="hv lv-poison"><span class="hv-k">hallucinated</span>'
            f"{_chips(h.hallucinated, 'chip chip-diff')}"
            '<span class="hv-note">verified claims the prose does not make — '
            "nothing downstream would catch these</span></div>"
        )
    if h.missing:
        parts.append(
            f'<div class="hv lv-warn"><span class="hv-k">missing</span>{_chips(h.missing)}'
            '<span class="hv-note">stated in the prose, not extracted</span></div>'
        )
    if h.unstable:
        parts.append(
            f'<div class="hv lv-warn"><span class="hv-k">unstable</span>{_chips(h.unstable)}'
            '<span class="hv-note">extracted in some trials and not others — not averaged away, '
            "because a field found 2 times in 3 is not a field you can depend on</span></div>"
        )
    if h.matched:
        parts.append(
            f'<div class="hv lv-ok"><span class="hv-k">matched</span>{_chips(h.matched)}</div>'
        )
    if h.n_rejected:
        parts.append(
            f'<div class="hv lv-none"><span class="hv-k">rejected</span>'
            f'<span class="tabular-nums font-semibold">{h.n_rejected}</span>'
            '<span class="hv-note">drafts the span-verification tripwire threw out — '
            "the safety net working, not a failure</span></div>"
        )
    if h.extracted:
        rows = "".join(
            f'<tr><td class="align-top"><code class="path">{esc(k)}</code></td>'
            f'<td class="align-top">{_value_html(v, None)}</td></tr>'
            for k, v in sorted(h.extracted.items())
        )
        parts.append(
            '<details class="drawer"><summary>everything the model extracted '
            f"({len(h.extracted)})</summary>"
            f'<div class="scroll-x mt-2"><table class="w-full text-sm"><tbody>{rows}'
            "</tbody></table></div></details>"
        )
    return f'<div class="sub-h">harvest</div><div class="hv-list">{"".join(parts)}</div>'


def _meta(case: CaseView) -> str:
    """The per-case costs, on one line: fields, trials, seconds, calls, tokens."""
    bits: list[str] = []
    if case.fields:
        n_ok = len(case.fields) - len(case.bad_fields)
        cls = "meta meta-bad" if case.bad_fields else "meta"
        bits.append(
            f'<span class="{cls}"><b class="tabular-nums">{n_ok}/{len(case.fields)}</b> '
            "fields</span>"
        )
    if case.trials > 1:
        n_ok = round(case.stability * case.trials)
        cls = "meta meta-bad" if case.unstable_across_trials else "meta"
        bits.append(
            f'<span class="{cls}"><b class="tabular-nums">{n_ok}/{case.trials}</b> '
            "trials correct</span>"
        )
    if case.missed_question:
        bits.append('<span class="meta meta-bad">missed a question it had to ask</span>')
    bits.append(f'<span class="meta"><b class="tabular-nums">{case.seconds:.1f}s</b></span>')
    if case.llm_calls:
        bits.append(
            f'<span class="meta"><b class="tabular-nums">{case.llm_calls}</b> LLM calls</span>'
        )
    for key, label in (
        ("input_tokens", "in"),
        ("output_tokens", "out"),
        ("cache_read_tokens", "cached"),
    ):
        if case.usage.get(key):
            bits.append(
                f'<span class="meta"><b class="tabular-nums">{case.usage[key]:,}</b> '
                f"{label} tokens</span>"
            )
    return f'<div class="meta-row">{"".join(bits)}</div>'


def _case_card(case: CaseView) -> str:
    if case.skipped is not None:
        return (
            '<div class="case lv-skip" data-level="skip">'
            '<div class="case-head">'
            f'<code class="case-id">{esc(case.case_id)}</code>'
            '<span class="pill lv-skip">skipped</span>'
            '<span class="outcome ml-auto">excluded from every rate</span>'
            "</div>"
            f'<p class="skip-why"><b>why:</b> {esc(case.skipped)}</p>'
            "</div>"
        )

    grade = case.grade or "?"
    level = case.level
    body = "".join(
        [
            "".join(f'<p class="note">{esc(n)}</p>' for n in case.notes),
            _field_table(case.fields),
            _harvest_block(case.harvest) if case.harvest and case.harvest.worth_showing else "",
            _meta(case),
        ]
    )
    # Failures open; a correct case collapses. `open` is an attribute rather than JS, so the page
    # is already in the right state before any script runs — and printing keeps it.
    is_open = " open" if level != "ok" else ""
    return (
        f'<details class="case lv-{esc(level)}" data-level="{esc(level)}"{is_open}>'
        '<summary class="case-head">'
        f'<code class="case-id">{esc(case.case_id)}</code>'
        f'<span class="pill lv-{esc(level)}">{esc(grade)}</span>'
        f'<span class="outcome">{esc(case.expected)} <span class="arrow">&rarr;</span> '
        f"{esc(case.actual)}</span>"
        '<span class="marker ml-auto" aria-hidden="true"></span>'
        "</summary>"
        f'<div class="case-body">{body}</div>'
        "</details>"
    )


def _legend(seen: list[str]) -> str:
    if not seen:
        return ""
    rows = "".join(
        f'<tr><td class="align-top"><span class="pill lv-{esc(GRADE_LEVEL.get(g, "warn"))}">'
        f'{esc(g)}</span></td><td class="align-top">{esc(GRADE_BLURB[g][0])}</td>'
        f'<td class="align-top text-dim">{esc(GRADE_BLURB[g][1])}</td></tr>'
        for g in GRADE_ORDER
        if g in seen
    )
    return (
        '<section class="panel"><h2 class="panel-h">The grade vocabulary</h2>'
        '<p class="panel-sub">Not all failures cost the same, so grading is a confusion matrix and '
        "not a pass/fail bit. Only the grades this run produced are listed.</p>"
        '<div class="scroll-x"><table class="w-full text-sm">'
        '<thead><tr><th class="text-left">grade</th><th class="text-left">meaning</th>'
        '<th class="text-left">cost</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div></section>"
    )


def _distribution(counts: Counter[str], n_skipped: int) -> str:
    """A single bar: the whole graded run, worst segment first. Width is the share; colour the level.

    A run with nothing graded says so rather than drawing an empty bar — an all-skipped benchmark tier
    (every package unreachable) is a legible outcome and must not look like a run with no failures.
    """
    total = sum(counts.values())
    if not total:
        return (
            '<p class="panel-sub">Nothing was graded'
            + (f" — all {n_skipped} cases skipped." if n_skipped else ".")
            + "</p>"
        )
    segments = "".join(
        f'<span class="seg lv-{esc(GRADE_LEVEL.get(g, "warn"))}" '
        f'style="width:{counts[g] / total:.4%}" title="{esc(g)}: {counts[g]}"></span>'
        for g in GRADE_ORDER
        if counts[g]
    )
    keys = "".join(
        f'<span class="key"><span class="sw lv-{esc(GRADE_LEVEL.get(g, "warn"))}"></span>'
        f'<b class="tabular-nums">{counts[g]}</b> {esc(g)}</span>'
        for g in GRADE_ORDER
        if counts[g]
    )
    if n_skipped:
        keys += (
            f'<span class="key"><span class="sw lv-skip"></span>'
            f'<b class="tabular-nums">{n_skipped}</b> skipped '
            "<span class='text-dim'>(excluded from every rate)</span></span>"
        )
    return f'<div class="bar">{segments}</div><div class="keys">{keys}</div>'


# --------------------------------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------------------------------


def _tiles(report: dict[str, Any], cases: list[CaseView], false_accepts: list[CaseView]) -> str:
    graded = [c for c in cases if c.skipped is None]
    skipped = [c for c in cases if c.skipped is not None]
    cost: dict[str, Any] = report.get("cost", {}) or {}
    questions: dict[str, Any] = report.get("questions_asked", {}) or {}

    accuracy = report.get("field_accuracy")
    # `build_report`'s own denominator, reproduced rather than approximated: the field checks PLUS
    # the harvest fields (matched + missing + hallucinated), because a verified-but-wrong assertion
    # is a failed check like any other. Counting only `fields` would print a fraction beside a
    # percentage that does not equal it.
    n_checks = sum(len(c.fields) for c in graded)
    n_ok = sum(1 for c in graded for f in c.fields if f.ok)
    for c in graded:
        if c.harvest is not None:
            n_checks += (
                len(c.harvest.matched) + len(c.harvest.missing) + len(c.harvest.hallucinated)
            )
            n_ok += len(c.harvest.matched)
    false_refuse = sum(1 for c in graded if c.grade == "false_refuse")
    missed = int(questions.get("missed", 0) or 0)

    work = float(cost.get("seconds", 0.0) or 0.0)
    wall = cost.get("wall_seconds")
    tokens = sum(
        int(cost.get(k, 0) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens")
    )

    tiles = [
        _tile(
            "cases graded",
            f"{len(graded)}",
            f"{len(skipped)} skipped" if skipped else "none skipped",
        ),
        _tile(
            "false accepts",
            f"{len(false_accepts)}",
            "exit 3 on any — a count, never a rate",
            "poison" if false_accepts else "ok",
        ),
        _tile(
            "field accuracy",
            "—" if accuracy is None else f"{float(accuracy):.1%}",
            f"{n_ok} of {n_checks} checks matched" if n_checks else "nothing to check in this tier",
            "bad" if n_ok < n_checks else "ok",
        ),
        _tile(
            "false refuses",
            f"{false_refuse}",
            "throughput cost only — a human unblocks it",
            "bad" if false_refuse else "none",
        ),
        _tile(
            "questions asked",
            _count(float(questions.get("total", 0) or 0)),
            f"{missed} needed and not asked" if missed else "none missed",
            "poison" if missed else "none",
        ),
        _tile(
            "work done",
            _seconds(work),
            "sum of the per-case durations",
        ),
    ]
    # The pair that the old renderer collapsed into one number labelled "wall time". They are
    # different questions: the sum says the corpus got more expensive, the elapsed says the run got
    # faster. An older report predates `wall_seconds`, so its absence is stated rather than faked.
    if wall is not None:
        wall_f = float(wall)
        speedup = f"{work / wall_f:.1f}× parallel" if wall_f > 0 else "elapsed"
        tiles.append(_tile("elapsed", _seconds(wall_f), f"wall clock — {speedup}"))
    else:
        tiles.append(_tile("elapsed", "—", "not recorded by this run"))
    tiles.append(
        _tile(
            "LLM calls",
            _count(float(cost.get("llm_calls", 0) or 0)),
            f"{tokens:,} tokens" if tokens else "none — this tier is deterministic",
        )
    )
    return f'<div class="tiles">{"".join(tiles)}</div>'


def render_html(
    report: dict[str, Any],
    *,
    title: str = "seqforge eval report",
    source: str | None = None,
    generated_at: str | None = None,
) -> str:
    """Render ``report`` (the JSON ``eval run`` emits) to one complete, self-contained document.

    ``source`` is the command that produced the report; it is rendered verbatim in the header,
    because a report without its command is a number whose provenance has to be reconstructed.
    ``generated_at`` is optional so the output can be byte-reproducible in a test.
    """
    cases = sorted(
        (_case_view(c) for c in report.get("per_case", [])),
        key=lambda c: (c.rank, c.case_id),
    )
    graded = [c for c in cases if c.skipped is None]
    false_accepts = [c for c in graded if c.grade == "false_accept"]
    counts: Counter[str] = Counter(c.grade or "?" for c in graded)
    n_skipped = len(cases) - len(graded)
    n_fail = sum(1 for c in graded if c.level != "ok")

    src_line = (
        '<div class="src"><span class="src-k">produced by</span>'
        f'<code class="val">{esc(source)}</code></div>'
        if source
        else ""
    )
    stamp = f" · {esc(generated_at)}" if generated_at else ""

    return _fill(
        _asset("eval-report.html"),
        TITLE=esc(title),
        CSS=_asset("eval-report.css"),
        JS=_script_guard(_asset("eval-report.js")),
        SOURCE=src_line,
        VERDICT=_banner(false_accepts),
        TILES=_tiles(report, cases, false_accepts),
        DISTRIBUTION=_distribution(counts, n_skipped),
        CASES="".join(_case_card(c) for c in cases),
        LEGEND=_legend(list(counts)),
        FOOTER=(
            f"seqforge eval report v{esc(EVAL_REPORT_VERSION)}{stamp} · "
            f"{len(graded)} graded, {n_skipped} skipped, {n_fail} not correct · "
            "rendered from the JSON <code class='val'>seqforge eval run</code> emits, with every "
            "asset inlined — this page fetches nothing."
        ),
    )


__all__ = [
    "EVAL_REPORT_VERSION",
    "GRADE_BLURB",
    "GRADE_LEVEL",
    "GRADE_ORDER",
    "render_html",
]
