"""Render one ``seqforge eval run`` report as a single self-contained HTML page.

The input is the JSON ``eval run`` already writes to stdout — this module is a *consumer* of that
stream, never a second output mode for it (ADR-0013 makes machine JSON the contract and forbids a
``--json`` switch). Same shape as
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

**A skip is a state, not an omission — and there are two of them.** An unreachable benchmark package is
a real outcome of the HF tier. Skips render with their reason on the face of the card and are excluded
from every rate, exactly as ``build_report`` excludes them — a skip is never a pass. A package the
archive answered *404* for is labelled **absent** instead: it was never published, so the case cannot
run anywhere for anyone, and the fix is to publish it rather than to try again later. Both stay outside
every rate; only one is an instruction, and a page that spelled them the same way let a dataset go
quietly missing behind a word that reads as transient.

**And a harvest stage that did not run is a third state, on the same argument.** A case whose
documents the model never answered still grades its byte half, so it is not a skip — but its
``matched`` list is empty for a reason no reader could otherwise see. Every harvest block carries a
``status``, the page counts the three across the tier in a tile of their own, and a field nothing
that answered was asked for renders as **unchecked** rather than as one the model failed to find.

**Both times, labelled.** ``cost.seconds`` is the sum of the per-case durations (work done);
``cost.wall_seconds`` is the elapsed time. Under the parallel runner they differ by the fan-out, and
calling the sum "wall time" — as this renderer's predecessor did — reports a run as ten times slower
than it was.

**A claim is shown with the quote it rests on, and a refusal is shown at all.** The harvest grade
carries the Assertions themselves, so the page can say *from this quote, in this document, at these
offsets* rather than only ``field = value``; and the drafts the tripwire threw out are a readable
list rather than an integer, because a count says a net caught something and nothing about what.

**The transcript is sampled, and the page says so.** :func:`attach_transcripts` folds the
``.jsonl`` files beside the report into it before rendering — the system prompt once (it is
byte-identical across a run, which is what makes prefix caching work), then a *representative*
selection of exchanges per case. Rendering stays a pure function of one dict; what varies is how
much was folded in, and every fragment that shows less than everything states what it left out. A
silently truncated transcript reads as a complete one, which is the failure this page is built
against.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from importlib.resources import files
from pathlib import Path
from typing import Any

from ..harvest.extract import document_sha256_in
from ..harvest.meter import RAW_KEYS, Exchange
from ..harvest.transcript import read_transcript

#: Bumped when the page's layout or projection changes. Not folded into any content-addressed key —
#: the report is a rebuildable view of a JSON file, never an input to anything.
EVAL_REPORT_VERSION = "2026.8.1"

#: What ``--transcript`` accepts. ``sample`` is the default: see :func:`select_exchanges` for what a
#: sample is and :func:`_exchange_block` for where the page states what it left out.
TRANSCRIPT_MODES = ("all", "sample", "none")

#: The most exchanges one case contributes under ``sample``. Twelve because that is roughly what a
#: reader will actually open, and because the alternative is not "all of them" but a page nobody can
#: load: one benchmark case has been measured at 983 exchanges, and every exchange carries a document.
#: Scope representatives are added on TOP of this (there are at most five scopes), so the cheap
#: "one per level" guarantee is never the thing a cap eats.
SAMPLED_EXCHANGES = 12

#: How much of each half of one exchange the page renders. The transcript file is the full text's
#: address; this is a view over it. Wherever it bites, the page says so — a clipped document that
#: did not admit it is the same lie as a truncated transcript that reads as a complete one.
EXCHANGE_CHARS = 800

#: The system prompt is rendered ONCE per report, so it can afford to be whole. The bound is a
#: guard against a pathological prompt, not a display choice.
PROMPT_CHARS = 24_000

#: Refused drafts rendered per case. One case has produced 84; past a few dozen a reader is
#: skimming, and the JSON has all of them.
REJECTED_ROWS = 40

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
# the transcript: the exchanges live in files beside the report, and are folded in before rendering
# --------------------------------------------------------------------------------------------------


def attach_transcripts(
    report: dict[str, Any], run_dir: Path | None, *, mode: str = "sample"
) -> dict[str, Any]:
    """Fold the ``.jsonl`` transcripts beside ``report`` into it, and return a NEW report dict.

    A transcript cannot ride on stdout — stdout *is* the result object — so ``eval run`` writes it to
    a file and the report names the path. That leaves the renderer with two inputs and one seam: this
    reads the files the report points at and puts a bounded, selected view of them **into** the same
    dict, so :func:`render_html` stays a pure function of one object and a fixture can exercise every
    branch of the page without a directory on disk.

    Absent, never empty: a report that named no transcript, an ``--no-llm`` run, a report that
    arrived over a pipe with no directory beside it, and ``mode="none"`` all come back unchanged, and
    the page renders without an exchanges section rather than with an empty one.
    """
    if mode not in TRANSCRIPT_MODES:
        raise ValueError(f"unknown transcript mode {mode!r}: expected one of {TRANSCRIPT_MODES}")
    if mode == "none" or run_dir is None:
        return report

    prompts: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    attached = False
    for raw in report.get("per_case", []):
        relative = raw.get("transcript")
        if not isinstance(relative, str):
            rows.append(raw)
            continue
        try:
            transcript = read_transcript(run_dir / relative)
        except (OSError, ValueError):
            # Named and unreadable — a downloaded artifact missing a file, or a half-written one.
            # The row keeps its path and the page says the exchanges were not read.
            rows.append(raw)
            continue
        harvest = raw.get("harvest") or {}
        chosen = select_exchanges(
            transcript.exchanges,
            scopes={
                str(d.get("doc_sha256")): str(d.get("scope", "?"))
                for d in harvest.get("documents", [])
            },
            claimed={
                str(a.get("span", {}).get("doc_sha256")) for a in harvest.get("assertions", [])
            },
            refused={str(r.get("doc_sha256")) for r in harvest.get("rejected", [])},
            limit=None if mode == "all" else SAMPLED_EXCHANGES,
        )
        rows.append(
            {
                **raw,
                # The document each exchange was about is resolved HERE, from the one line the
                # prompt writes it on, so the projection below never has to parse a prompt.
                "exchanges": [
                    {**x.to_json(), "why": why, "doc_sha256": document_sha256_in(x.user)}
                    for x, why in chosen
                ],
                "n_exchanges": transcript.n_exchanges,
            }
        )
        attached = True
        for sha, text in transcript.prompts.items():
            entry = prompts.setdefault(sha, {"sha256": sha, "text": text, "n_exchanges": 0})
            entry["n_exchanges"] += sum(1 for x in transcript.exchanges if x.prompt_sha256 == sha)

    if not attached:
        return report
    return {**report, "per_case": rows, "prompts": list(prompts.values())}


def select_exchanges(
    exchanges: Sequence[Exchange],
    *,
    scopes: Mapping[str, str],
    claimed: set[str],
    refused: set[str],
    limit: int | None,
) -> list[tuple[Exchange, str]]:
    """The representative selection, and the reason each exchange survived it.

    A corpus-scale run produces hundreds of exchanges and the page is one inlined file, so showing
    all of them is not an option and showing the first N is not a sample — it is whatever the thread
    pool happened to finish first. What has signal is:

    - **every exchange that failed.** It produced neither a draft nor a claim, so no other rule below
      would keep it, and it is the one exchange whose whole story is "we paid and got nothing back";
    - **every exchange whose document produced a refused draft** — where the tripwire fired;
    - **every exchange whose document produced a graded claim** — where the evidence came from;
    - **one exchange per document scope**, so a reader sees what a ``sample`` record's ask looks like
      even in a run where every sample answered cleanly.

    The scope representatives are chosen *after* the rest and never compete with them for the cap:
    the point of that rule is coverage, and it costs at most five exchanges. ``limit`` bounds the
    signal-selected ones only; ``None`` (``--transcript all``) keeps everything. Order is the
    transcript's own throughout, so the sample reads as a subsequence of the run rather than as a
    ranking.

    The join is by document, and a retry is its own exchange: two exchanges for one document are both
    kept, which is the honest answer — one of them is a request that was paid for twice.
    """
    kept: dict[int, str] = {}
    docs = [document_sha256_in(x.user) for x in exchanges]

    for i, exchange in enumerate(exchanges):
        sha = docs[i]
        if exchange.error is not None:
            kept[i] = "the request failed"
        elif sha is not None and sha in refused:
            kept[i] = "produced a rejected draft"
        elif sha is not None and sha in claimed:
            kept[i] = "produced a graded assertion"
    if limit is not None and len(kept) > limit:
        kept = dict(sorted(kept.items())[:limit])

    covered = {scopes.get(docs[i] or "", "") for i in kept}
    for i, sha in enumerate(docs):
        scope = scopes.get(sha or "")
        if i in kept or scope is None or scope in covered:
            continue
        covered.add(scope)
        kept[i] = f"the first {scope}-scoped document"
    return [(exchanges[i], kept[i]) for i in sorted(kept)]


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
class DocView:
    """One document a case sent, so a sha256 in a span resolves back to something a human reads."""

    sha256: str
    source: str
    scope: str
    subject: str | None
    n_chars: int
    #: The provider's own message where this document was never answered; ``None`` where it was.
    #: An empty batch is an answer and leaves this ``None``, which is the whole distinction.
    failure: str | None = None

    @property
    def label(self) -> str:
        """``GSM4318946`` where the document speaks for a record, its basename where it does not."""
        return self.subject or self.source


@dataclass(frozen=True)
class ClaimView:
    """One Assertion the harvest grade was computed over, with the quote it rests on."""

    field: str
    value: str
    quote: str
    doc_sha256: str
    char_start: int | None
    char_end: int | None
    page: int | None
    confidence: float | None

    @property
    def where(self) -> str:
        """The span, as a reader checks it: characters into the canonical text, and a PDF page."""
        bits = []
        if self.char_start is not None and self.char_end is not None:
            bits.append(f"chars {self.char_start:,}–{self.char_end:,}")
        if self.page is not None:
            bits.append(f"p.{self.page}")
        return " · ".join(bits)


@dataclass(frozen=True)
class RefusalView:
    """One draft that did not survive. Every field may be absent: a malformed draft is malformed."""

    field: str | None
    value: str | None
    quote: str | None
    reason: str
    detail: str
    doc_sha256: str | None


@dataclass(frozen=True)
class ExchangeView:
    """One request and what came back, as the page shows it."""

    doc_sha256: str | None
    user: str
    text: str
    model: str
    error: str | None
    why: str
    usage: dict[str, int]

    @property
    def tokens(self) -> int:
        return sum(int(self.usage.get(k, 0) or 0) for k in RAW_KEYS)


@dataclass(frozen=True)
class HarvestView:
    """How the LLM stage did on one case. Absent under ``--no-llm``, where nothing harvested."""

    matched: list[str]
    missing: list[str]
    hallucinated: list[str]
    unstable: list[str]
    #: Expected fields nothing that answered was asked for. Could not check, not checked and found
    #: nothing — and a page that renders it as ``missing`` blames a model for a document it was
    #: never shown.
    unchecked: list[str]
    n_rejected: int
    claims: list[ClaimView]
    refusals: list[RefusalView]
    documents: dict[str, DocView]
    mode: dict[str, Any]
    #: ``complete`` | ``partial`` | ``unmeasured``. Absent from a report written before this field,
    #: which is read as ``complete`` — that is what such a report meant, since a case whose model
    #: failed did not appear as a graded case at all.
    status: str = "complete"
    n_documents: int = 0
    n_documents_failed: int = 0

    @property
    def unanswered(self) -> list[DocView]:
        """The documents the provider never answered, each with its own reason."""
        return [d for d in self.documents.values() if d.failure is not None]

    def source_of(self, doc_sha256: str | None) -> str:
        """What a document sha reads as — its subject and level, or a short sha if it is unknown."""
        doc = self.documents.get(doc_sha256 or "")
        if doc is None:
            return f"{doc_sha256[:8]}…" if doc_sha256 else "unknown document"
        return f"{doc.label} · {doc.scope}"

    @property
    def worth_showing(self) -> bool:
        return bool(
            self.matched
            or self.missing
            or self.hallucinated
            or self.unstable
            or self.unchecked
            # A stage that did not finish is the case most worth showing and the one with least to
            # show: every list above it is empty precisely because it did not run.
            or self.status != "complete"
            or self.n_rejected
            # A case that extracted claims nothing expected still extracted them, and hiding those
            # behind an empty scorecard is how a corpus grows a field nobody knew it was producing.
            or self.claims
        )


@dataclass(frozen=True)
class CaseView:
    """One row of ``per_case``. ``skipped`` set means every graded field is absent, not zero."""

    case_id: str
    grade: str | None
    skipped: str | None
    #: ``absent`` when the corpus does not hold this case's package at all — never published, so it
    #: cannot run anywhere. Anything else is this machine's accident. An older report predates the
    #: field and reads as ``unavailable``, which is what it always meant.
    skip_kind: str
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
    #: The exchanges folded in by :func:`attach_transcripts` — a selection, not the run.
    exchanges: list[ExchangeView]
    #: How many there were in total, so the page can say what it left out.
    n_exchanges: int
    #: Where they live, RELATIVE to the run directory. Set whenever the case reached a model, even
    #: when nothing was read back — "recorded, not shown" is a different sentence from "none".
    transcript: str | None

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
    def is_absent(self) -> bool:
        """The package was never published. A finding about the corpus, not about this run."""
        return self.skipped is not None and self.skip_kind == "absent"

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
        skip_kind=str(raw.get("skip_kind") or "unavailable"),
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
        harvest=None if harvest is None else _harvest_view(harvest),
        seconds=float(raw.get("seconds", 0.0) or 0.0),
        llm_calls=int(raw.get("llm_calls", 0) or 0),
        usage={str(k): int(v) for k, v in raw.get("usage", {}).items()},
        exchanges=[_exchange_view(x) for x in raw.get("exchanges", [])],
        n_exchanges=int(raw.get("n_exchanges", 0) or 0),
        transcript=raw.get("transcript"),
    )


def _harvest_view(harvest: dict[str, Any]) -> HarvestView:
    """Project the harvest grade, from a report of any age.

    Every read is a ``.get`` because the shape moved: a report written before the grade carried its
    claims has a flat ``extracted`` dict and an integer ``n_rejected``, and it still renders — with
    the values it recorded and without the quotes it never had. A renderer that could only read the
    current shape would make the committed benchmark reports unreadable on the day the shape changed.
    """
    documents = {
        str(d.get("doc_sha256", "")): DocView(
            sha256=str(d.get("doc_sha256", "")),
            source=str(d.get("source", "?")),
            scope=str(d.get("scope", "?")),
            subject=(str(d["subject"]) if d.get("subject") else None),
            n_chars=int(d.get("n_chars", 0) or 0),
            failure=(str(d["failure"]) if d.get("failure") else None),
        )
        for d in harvest.get("documents", [])
    }
    claims = [
        ClaimView(
            field=str(a.get("field", "?")),
            value=str(a.get("value", "")),
            quote=str((a.get("span") or {}).get("quote", "")),
            doc_sha256=str((a.get("span") or {}).get("doc_sha256", "")),
            char_start=(a.get("span") or {}).get("char_start"),
            char_end=(a.get("span") or {}).get("char_end"),
            page=(a.get("span") or {}).get("page"),
            confidence=a.get("llm_confidence"),
        )
        for a in harvest.get("assertions", [])
    ]
    claims += [
        # The older shape, kept legible rather than dropped: a value with no quote beside it, which
        # is exactly what that report recorded.
        ClaimView(
            field=str(k),
            value=str(v),
            quote="",
            doc_sha256="",
            char_start=None,
            char_end=None,
            page=None,
            confidence=None,
        )
        for k, v in harvest.get("extracted", {}).items()
    ]
    refusals = [
        RefusalView(
            field=(str(r["field"]) if r.get("field") else None),
            value=(str(r["value"]) if r.get("value") else None),
            quote=(str(r["quote"]) if r.get("quote") else None),
            reason=str(r.get("reason", "?")),
            detail=str(r.get("detail", "")),
            doc_sha256=(str(r["doc_sha256"]) if r.get("doc_sha256") else None),
        )
        for r in harvest.get("rejected", [])
    ]
    return HarvestView(
        matched=[str(x) for x in harvest.get("matched", [])],
        missing=[str(x) for x in harvest.get("missing", [])],
        hallucinated=[str(x) for x in harvest.get("hallucinated", [])],
        unstable=[str(x) for x in harvest.get("unstable", [])],
        unchecked=[str(x) for x in harvest.get("unchecked", [])],
        # From the list where there is one, from the old integer where there is not.
        n_rejected=len(refusals) or int(harvest.get("n_rejected", 0) or 0),
        claims=claims,
        refusals=refusals,
        documents=documents,
        mode={str(k): v for k, v in (harvest.get("mode") or {}).items()},
        status=str(harvest.get("status") or "complete"),
        n_documents=int(harvest.get("n_documents", 0) or 0),
        n_documents_failed=int(harvest.get("n_documents_failed", 0) or 0),
    )


def _exchange_view(raw: dict[str, Any]) -> ExchangeView:
    return ExchangeView(
        doc_sha256=(str(raw["doc_sha256"]) if raw.get("doc_sha256") else None),
        user=str(raw.get("user", "")),
        text=str(raw.get("text", "")),
        model=str(raw.get("model", "")),
        error=(str(raw["error"]) if raw.get("error") else None),
        why=str(raw.get("why", "")),
        usage={str(k): int(v) for k, v in (raw.get("usage") or {}).items()},
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
    """The LLM stage's own scorecard. ``hallucinated`` is corpus poison; ``n_rejected`` is the net.

    The verdicts come first as chips, then the two drawers they are verdicts *about*: the drafts the
    net caught, and the claims that got through with the quote each rests on. A scorecard whose
    evidence is one drawer away is checkable; the same scorecard with the evidence discarded on the
    way in is a number to be believed.
    """
    parts = []
    if h.status != "complete":
        # First, because it qualifies every chip under it. An empty `matched` beneath a stage that
        # never ran is not the same reading as an empty `matched` beneath one that did.
        n_ok = h.n_documents - h.n_documents_failed
        why = "; ".join(f"{d.label} · {d.scope}: {d.failure}" for d in h.unanswered if d.failure)
        parts.append(
            f'<div class="hv lv-warn"><span class="hv-k">{esc(h.status)}</span>'
            f'<span class="tabular-nums font-semibold">{n_ok} of {h.n_documents}</span>'
            '<span class="hv-note">documents answered — this scorecard is a claim about those '
            f"only{(' · ' + esc(why)) if why else ''}</span></div>"
        )
    if h.unchecked:
        parts.append(
            f'<div class="hv lv-warn"><span class="hv-k">unchecked</span>{_chips(h.unchecked)}'
            '<span class="hv-note">nothing that answered was asked these — could not check, '
            "not checked and found nothing; excluded from every rate</span></div>"
        )
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
    parts.append(_refusals_drawer(h))
    parts.append(_claims_drawer(h))
    return f'<div class="sub-h">harvest</div><div class="hv-list">{"".join(parts)}</div>'


def _claims_drawer(h: HarvestView) -> str:
    """Every claim that survived, with the quote it rests on and where that quote is.

    **What is deliberately NOT a column: the two verification flags.** ``span_verified`` and
    ``entailment_ok`` are code-owned and an Assertion is only constructed once both hold, so a
    per-row pair of ticks would be two columns of the constant ``true`` — evidence-shaped and
    carrying nothing. The invariant is worth stating and it is stated once, in the line above the
    table, where it also points at the refusals: the drafts where those checks did something are the
    ones that FAILED them, and they are in the drawer above this one.

    What varies per row, and is therefore what a row shows: the quote (the model's contribution, and
    the only part a reader can independently check), where the quote sits (code's contribution),
    which document it came from, and the model's own confidence — the one number on an Assertion
    that is not a verdict code reached.
    """
    if not h.claims:
        return ""
    rows = "".join(
        f'<tr><td class="align-top"><code class="path">{esc(c.field)}</code></td>'
        f'<td class="align-top">{_value_html(c.value, None)}</td>'
        f'<td class="align-top">{_quote_cell(c, h)}</td>'
        + '<td class="align-top tabular-nums text-dim">'
        + (f"{c.confidence:.2f}" if c.confidence is not None else "—")
        + "</td></tr>"
        for c in sorted(h.claims, key=lambda c: (c.field, c.value))
    )
    return (
        f'<details class="drawer"><summary>the {len(h.claims)} claim(s) that survived, with the '
        "quote each rests on</summary>"
        '<p class="hv-note mt-2">Every row is span-verified <b>and</b> entailment-checked: an '
        "Assertion is only built once both hold, so the flags are stated here rather than repeated "
        "as two true columns. Where those checks did something is the rejected drawer above.</p>"
        '<div class="scroll-x mt-2"><table class="w-full text-sm">'
        '<thead><tr><th class="text-left">field</th><th class="text-left">value</th>'
        '<th class="text-left">quote it rests on</th><th class="text-left">conf</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div></details>"
    )


def _quote_cell(claim: ClaimView, h: HarvestView) -> str:
    """The quote, then where it is — document, offsets, page. Empty for an older report."""
    if not claim.quote:
        return '<span class="nil">not recorded by this run</span>'
    where = " · ".join(x for x in (h.source_of(claim.doc_sha256), claim.where) if x)
    return f'<code class="quote">{esc(claim.quote)}</code><span class="hv-note">{esc(where)}</span>'


def _refusals_drawer(h: HarvestView) -> str:
    """The drafts that did not survive, read as claims rather than counted as a number.

    Two producers land in one list and the reason tells them apart: ``malformed_draft`` is a draft
    the model returned broken (so its field, value and quote may all be absent, and the row says so
    rather than inventing them), and everything else is a draft whose field, document, quote or
    entailment failed. Sorted by reason, because the question a reader has is which check fired.
    """
    if not h.refusals:
        return ""
    shown = sorted(h.refusals, key=lambda r: (r.reason, r.field or ""))[:REJECTED_ROWS]
    rows = "".join(
        f'<tr><td class="align-top"><code class="chip chip-diff">{esc(r.reason)}</code></td>'
        f'<td class="align-top">{_or_nil(r.field, "path")}</td>'
        f'<td class="align-top">{_or_nil(r.value, "val")}</td>'
        f'<td class="align-top">{_or_nil(r.quote, "quote")}'
        f'<span class="hv-note">{esc(h.source_of(r.doc_sha256))}</span></td>'
        f'<td class="align-top text-dim">{esc(r.detail)}</td></tr>'
        for r in shown
    )
    dropped = (
        ""
        if len(shown) == len(h.refusals)
        else f'<p class="hv-note mt-2">Showing {len(shown)} of {len(h.refusals)} — the rest are in '
        "the report JSON.</p>"
    )
    return (
        f'<details class="drawer"><summary>the {len(h.refusals)} draft(s) the tripwire threw out'
        "</summary>"
        f"{dropped}"
        '<div class="scroll-x mt-2"><table class="w-full text-sm">'
        '<thead><tr><th class="text-left">reason</th><th class="text-left">field</th>'
        '<th class="text-left">value</th><th class="text-left">quote</th>'
        '<th class="text-left">detail</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div></details>"
    )


def _or_nil(value: str | None, cls: str) -> str:
    """A malformed draft may carry no field, no value and no quote. Say so; never print ``None``."""
    return f'<code class="{cls}">{esc(value)}</code>' if value else '<span class="nil">none</span>'


def _blob(label: str, text: str, limit: int) -> str:
    """One block of untrusted text — a document we sent, or whatever the model sent back.

    Everything in it is ``esc``aped: model output is not markup, and it will contain ``<``, ``&`` and
    sooner or later ``</script>``. Clipped to ``limit`` characters, and the clip is *stated*: a page
    that quietly showed the first 800 characters of a document would be a smaller version of the
    same lie as a transcript that shows the first twelve exchanges without saying so.
    """
    if not text:
        # An empty box reads as a broken renderer; "nothing" is a real answer here, and on the
        # returned half of a failed request it is the answer.
        return f'<div class="mt-2"><span class="src-k">{esc(label)}</span><span class="nil">nothing</span></div>'
    clipped = (
        ""
        if len(text) <= limit
        else f'<span class="hv-note">clipped — showing {limit:,} of {len(text):,} characters</span>'
    )
    return (
        f'<div class="mt-2"><span class="src-k">{esc(label)}</span>{clipped}'
        f'<pre class="blob">{esc(text[:limit])}</pre></div>'
    )


def _exchange_block(case: CaseView) -> str:
    """The case's transcript: a bounded selection of exchanges, and what it left out.

    An exchange is a request and the response it got, so it is a document plus a JSON batch — and a
    corpus-scale run makes hundreds of them. What is shown is :func:`select_exchanges`' sample, and
    the summary line above it names both numbers, because a silently truncated transcript reads as a
    complete one and that is the whole hazard here.

    The system prompt is NOT in any of these: it is byte-identical across every request in a run —
    which is what makes prefix caching work — so the page carries it once, in its own panel.
    """
    if not case.exchanges:
        if case.transcript and case.llm_calls:
            # Recorded, and not read: rendered from a bare report.json, from a pipe, or under
            # `--transcript none`. That is a different sentence from "this case reached no model".
            return (
                f'<div class="sub-h">exchanges</div><p class="hv-note">{case.llm_calls} exchange(s) '
                f'were recorded in <code class="path">{esc(case.transcript)}</code> and not read. '
                "Render the run directory (not the JSON alone) with <code class='val'>--transcript "
                "sample</code> to see them.</p>"
            )
        return ""

    total = max(case.n_exchanges, len(case.exchanges))
    dropped = (
        "every exchange"
        if len(case.exchanges) >= total
        else f"{len(case.exchanges)} of {total} exchanges"
    )
    cards = []
    for x in case.exchanges:
        source = case.harvest.source_of(x.doc_sha256) if case.harvest else (x.doc_sha256 or "?")
        head = (
            f'<code class="path">{esc(source)}</code>'
            f'<span class="hv-note">{esc(x.why)}</span>'
            f'<span class="hv-note tabular-nums">{x.tokens:,} tokens</span>'
        )
        if x.error:
            head += f'<span class="chip chip-diff">{esc(x.error)}</span>'
        cards.append(
            f'<details class="drawer"><summary>{head}</summary>'
            + _blob("sent", x.user, EXCHANGE_CHARS)
            + _blob("returned", x.text, EXCHANGE_CHARS)
            + "</details>"
        )
    return (
        f'<div class="sub-h">exchanges</div>'
        f'<p class="hv-note">Showing <b>{dropped}</b>. Each is one request and the response it got; '
        "the system prompt is identical across all of them and is at the foot of this page. The "
        f'unclipped run is in <code class="path">{esc(case.transcript or "the transcript")}</code>.'
        f"</p>{''.join(cards)}"
    )


def _prompt_panel(report: dict[str, Any]) -> str:
    """The system prompt, once per report.

    It is byte-identical across every request in a run — that is exactly why prefix caching works —
    so a transcript is one prompt plus N (document, response) pairs, not N full exchanges. Rendering
    it per exchange would be the same string a few hundred times, which is the difference between a
    page a human opens and one they do not.

    ``prompts`` is keyed by sha256 and normally holds one entry. More than one is a finding rather
    than a layout problem — the prefix cannot have been cached across them — so the page says so
    instead of picking the first.
    """
    prompts = [p for p in report.get("prompts", []) if p.get("text")]
    if not prompts:
        return ""
    version = (report.get("extractor") or {}).get("prompt_version")
    stamped = f" <code class='val'>{esc(version)}</code>" if version else ""
    drawers = "".join(
        '<details class="drawer"><summary>'
        f'<code class="path">{esc(str(p.get("sha256", ""))[:12])}…</code>'
        f'<span class="hv-note">{len(str(p.get("text", ""))):,} characters · '
        f"{int(p.get('n_exchanges', 0) or 0):,} exchange(s)</span></summary>"
        + _blob("system prompt", str(p.get("text", "")), PROMPT_CHARS)
        + "</details>"
        for p in prompts
    )
    split = (
        ""
        if len(prompts) == 1
        else f'<p class="panel-sub">This run issued <b>{len(prompts)}</b> distinct system prompts, '
        "so the stable prefix cannot have been cached across them.</p>"
    )
    return (
        f'<section class="panel"><h2 class="panel-h">The system prompt{stamped}</h2>'
        '<p class="panel-sub">Sent with every request, byte for byte — which is what makes prefix '
        "caching work, and why it is here once instead of beside each exchange above.</p>"
        f"{split}{drawers}</section>"
    )


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
            f'<span class="meta"><b class="tabular-nums">{case.llm_calls}</b> exchanges</span>'
        )
    # `input_tokens` is the WHOLE input on both providers; `cached` and `cache written` are the
    # breakdown of it, not extra tokens beside it. `cache_write_tokens` only the Anthropic path
    # reports, and every consumer but the on-disk ledger used to drop it.
    for key, label in (
        ("input_tokens", "in"),
        ("output_tokens", "out"),
        ("cache_read_tokens", "cached"),
        ("cache_write_tokens", "cache-written"),
    ):
        if case.usage.get(key):
            bits.append(
                f'<span class="meta"><b class="tabular-nums">{case.usage[key]:,}</b> '
                f"{label} tokens</span>"
            )
    # How the calls were made — the fourth thing an extraction outcome carries, and the one the eval
    # path used to drop. It belongs beside the token counts because it is the same kind of fact: not
    # what the model said, but what it was asked to do it under.
    if case.harvest is not None and case.harvest.mode:
        made = " · ".join(f"{k} {v}" for k, v in sorted(case.harvest.mode.items()))
        bits.append(f'<span class="meta">{esc(made)}</span>')
    return f'<div class="meta-row">{"".join(bits)}</div>'


def _case_card(case: CaseView) -> str:
    if case.skipped is not None:
        # Two skips, told apart on the face of the card. `absent` means the corpus does not hold this
        # case's package, so it cannot run anywhere for anyone — a gap somebody has to close, not a
        # bad day on the wire. Both stay outside every rate; only one is an instruction.
        pill, outcome = (
            ("absent", "never published — a gap in the corpus, excluded from every rate")
            if case.is_absent
            else ("skipped", "excluded from every rate")
        )
        level = "warn" if case.is_absent else "skip"
        # The one thing a skipped card still owes a reader. A case stopped at its token Ceiling
        # reports AS a skip — it was not graded — but the exchanges up to the breach were paid for,
        # and "on what?" is exactly the question a breach produces. A skip that reached no model has
        # no exchanges, so this is empty for every other skip there is.
        spent = f'<div class="case-body">{_exchange_block(case)}</div>' if case.exchanges else ""
        return (
            '<div class="case lv-skip" data-level="skip">'
            '<div class="case-head">'
            f'<code class="case-id">{esc(case.case_id)}</code>'
            f'<span class="pill lv-{level}">{pill}</span>'
            f'<span class="outcome ml-auto">{outcome}</span>'
            "</div>"
            f'<p class="skip-why"><b>why:</b> {esc(case.skipped)}</p>'
            f"{spent}</div>"
        )

    grade = case.grade or "?"
    level = case.level
    body = "".join(
        [
            "".join(f'<p class="note">{esc(n)}</p>' for n in case.notes),
            _field_table(case.fields),
            _harvest_block(case.harvest) if case.harvest and case.harvest.worth_showing else "",
            # After the scorecard and before the costs: the exchanges are the raw material the
            # harvest block is a view over, so they read as "and here is what that came from".
            _exchange_block(case),
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


def _distribution(counts: Counter[str], n_skipped: int, n_absent: int = 0) -> str:
    """A single bar: the whole graded run, worst segment first. Width is the share; colour the level.

    A run with nothing graded says so rather than drawing an empty bar — an all-skipped benchmark tier
    (every package unreachable) is a legible outcome and must not look like a run with no failures.

    ``n_absent`` is the subset of the skips whose package the corpus does not hold. It gets its own
    key rather than being folded into the skip count, because the two ask different things of a
    reader: one is "try again later", the other is "publish the package".
    """
    total = sum(counts.values())
    if not total:
        return (
            '<p class="panel-sub">Nothing was graded'
            + (f" — all {n_skipped} cases skipped." if n_skipped else ".")
            + (
                f" {n_absent} of them absent — the corpus does not hold those packages."
                if n_absent
                else ""
            )
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
    if n_skipped - n_absent:
        keys += (
            f'<span class="key"><span class="sw lv-skip"></span>'
            f'<b class="tabular-nums">{n_skipped - n_absent}</b> skipped '
            "<span class='text-dim'>(excluded from every rate)</span></span>"
        )
    if n_absent:
        keys += (
            f'<span class="key"><span class="sw lv-warn"></span>'
            f'<b class="tabular-nums">{n_absent}</b> absent '
            "<span class='text-dim'>(never published — a gap in the corpus)</span></span>"
        )
    return f'<div class="bar">{segments}</div><div class="keys">{keys}</div>'


# --------------------------------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------------------------------


def _tiles(report: dict[str, Any], cases: list[CaseView], false_accepts: list[CaseView]) -> str:
    graded = [c for c in cases if c.skipped is None]
    skipped = [c for c in cases if c.skipped is not None]
    absent = [c for c in skipped if c.is_absent]
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
    # Raw, and only two keys: `input_tokens` already includes the cached input and the cache writes
    # on both providers, so adding the breakdown back in would count the cached half twice. Same
    # arithmetic the token Ceiling uses, so the tile and the refusal agree about what a run cost.
    tokens = sum(int(cost.get(k, 0) or 0) for k in RAW_KEYS)

    tiles = [
        _tile(
            "cases graded",
            f"{len(graded)}",
            (
                f"{len(skipped)} skipped"
                + (f", {len(absent)} of them never published" if absent else "")
            )
            if skipped
            else "none skipped",
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
            "exchanges",
            _count(float(cost.get("llm_calls", 0) or 0)),
            f"{tokens:,} tokens" if tokens else "none — this tier is deterministic",
        )
    )
    # Only on a run that harvested. A `--no-llm` run has no stage to report coverage of, and a tile
    # reading "0 of 0 documents" would be a coverage failure rather than the absence of one.
    harvest: dict[str, Any] = report.get("harvest") or {}
    if harvest:
        planned = int(harvest.get("documents_planned", 0) or 0)
        got = int(harvest.get("documents_extracted", 0) or 0)
        unmeasured = int(harvest.get("cases_unmeasured", 0) or 0)
        partial = int(harvest.get("cases_partial", 0) or 0)
        unchecked = int(harvest.get("assertions_unchecked", 0) or 0)
        short = [
            f"{unmeasured} case(s) measured nothing" if unmeasured else "",
            f"{partial} partly" if partial else "",
            f"{unchecked} assertion(s) unchecked" if unchecked else "",
        ]
        tiles.append(
            _tile(
                "harvest coverage",
                f"{got} of {planned}",
                ", ".join(s for s in short if s) or "every planned document answered",
                "bad" if unmeasured or partial else "ok",
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
    n_absent = sum(1 for c in cases if c.is_absent)
    n_fail = sum(1 for c in graded if c.level != "ok")

    # The header line is the page's provenance, and the command alone is no longer enough of it: the
    # DeepSeek preset serves two V4 models and defaults to the cheap one, so the same command on the
    # same corpus can produce different numbers. `extractor` is absent on a `--no-llm` run (nothing
    # extracted) and on any report predating the field — both render as no chip rather than a guess.
    bits = []
    if source:
        bits.append(f'<span class="src-k">produced by</span><code class="val">{esc(source)}</code>')
    extractor: dict[str, Any] = report.get("extractor") or {}
    if extractor:
        who = f"{extractor.get('provider', '?')}/{extractor.get('model', '?')}"
        bits.append(f'<span class="src-k">extractor</span><code class="val">{esc(who)}</code>')
        if extractor.get("prompt_version"):
            bits.append(
                '<span class="src-k">prompt</span>'
                f'<code class="val">{esc(str(extractor["prompt_version"]))}</code>'
            )
    src_line = f'<div class="src">{"".join(bits)}</div>' if bits else ""
    stamp = f" · {esc(generated_at)}" if generated_at else ""

    return _fill(
        _asset("eval-report.html"),
        TITLE=esc(title),
        CSS=_asset("eval-report.css"),
        JS=_script_guard(_asset("eval-report.js")),
        SOURCE=src_line,
        VERDICT=_banner(false_accepts),
        TILES=_tiles(report, cases, false_accepts),
        DISTRIBUTION=_distribution(counts, n_skipped, n_absent),
        CASES="".join(_case_card(c) for c in cases),
        # Two trailing panels through one slot, deliberately: the template's sections are the page's
        # SHAPE, and "reference material after the cases" is one shape whether it holds one panel or
        # two. A second slot for a panel that renders only on an `--llm` run would put a decision
        # about what to show into the file that must not contain one.
        LEGEND=_legend(list(counts)) + _prompt_panel(report),
        FOOTER=(
            f"seqforge eval report v{esc(EVAL_REPORT_VERSION)}{stamp} · "
            f"{len(graded)} graded, {n_skipped} skipped"
            + (f" ({n_absent} absent)" if n_absent else "")
            + f", {n_fail} not correct · "
            "rendered from the JSON <code class='val'>seqforge eval run</code> emits, with every "
            "asset inlined — this page fetches nothing."
        ),
    )


__all__ = [
    "EVAL_REPORT_VERSION",
    "GRADE_BLURB",
    "GRADE_LEVEL",
    "GRADE_ORDER",
    "SAMPLED_EXCHANGES",
    "TRANSCRIPT_MODES",
    "attach_transcripts",
    "render_html",
    "select_exchanges",
]
