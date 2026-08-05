"""Records -> documents, planned before it is paid for — and the fan-out that then pays.

This module exists because the same twenty lines were written twice, verbatim, in
:mod:`seqforge.cli.harvest` and in ``evals/run.py``: filter the records that have prose *and* an ask,
render each one, fan the extractions out over a thread pool, reassemble. Two adapters at one seam
make it a real seam, and the seam was implicit — **a dataset's cost was a property nobody computed
until it had been paid**.

Three things follow from making it explicit, and each one was a defect before:

**The plan is exact, not an estimate of a shape.** Rendering a record costs no tokens and no network,
so :func:`plan_extraction` renders everything and hands back the documents themselves. "What will
this dataset cost" is then answerable without asking anybody — :func:`~seqforge.cli.harvest` exposes
it as ``harvest extract --dry-run``.

**Run aliases collapse per SAMPLE.** A run alias (``N2_wild_type``, ``GSM3618670_r1``) is often the
only place a contrast is written in plain words, so run records are read — see
:mod:`seqforge.harvest.fields` for the pilot that falsified the alternative. But a run belongs to
exactly one sample, and the full nine-attribute sample vocabulary asked of a one-line alias, once per
run, is what made one benchmark case 92% of a corpus-wide bill. The runs of one sample become **one**
document carrying that sample's accession, so every claim from it still resolves as ``asserted``
against that sample (:func:`seqforge.resolve.records._basis_for` does the join through
``subject_to_sample``, which maps a sample accession to itself). The defect was one call per run, not
reading runs.

**A document is identified by what will be asked of it.** The CLI reassembled its results into a dict
keyed by ``doc_sha256``, so two identical documents each cost a call, one result survived, and the
reassembly loop then read that single outcome once per colliding document — duplicating its drafts,
its rejected list and its usage. Here identical asks are one document, and the results come back as a
LIST in plan order, so there is no key to collide on.

**Documents that receive the same ask share a request.** A document is not a request: the ~9 KB system
prefix and the ~1.3 KB ask are byte-identical per request, so three archive records of 45, 209 and 213
characters cost ~9.4 K input tokens as three requests, >95 % of it prompt, paid three times over three
round trips (#190). :func:`batch_documents` is where that stops, and it is the only thing here that
decides how many requests a plan is.

**How many share one is a function of that same ask.** A request's width is an output budget divided
by what the ask can cost (:func:`batch_width`), so the two-field question travels wide and the
nine-attribute one does not. It used to be one number for both, and the narrow ask paid the wide
ask's price on every dataset since batching landed.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..models.assertion import ExtractionPlanReport, PlannedDocument
from ..models.records import ArchiveRecord, ArchiveRecordSet
from .extract import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    ExtractionOutcome,
    ExtractUnavailable,
    extract_batch,
    extract_drafts,
)
from .fields import DocScope, fields_for
from .meter import CHARS_PER_TOKEN
from .normalize import (
    NormalizedDoc,
    declared_spans,
    has_prose,
    normalize_record,
    normalize_text,
    render_record,
)

if TYPE_CHECKING:
    from ..kb.schema import Spec
    from .providers import LLMProvider

#: The most extraction requests this process may have in flight at once, however the pools nest above
#: it. Case-level and document-level concurrency MULTIPLY and the provider sees the product: 14 cases
#: x 24 documents is 336 requests in flight from one key, which measures the provider's rate limiter
#: and not the compiler. Sized once at import so every pool in the process shares one allowance.
MAX_IN_FLIGHT = min(24, (os.cpu_count() or 1) * 2)

_SLOTS = threading.Semaphore(MAX_IN_FLIGHT)

#: The most DOCUMENT TEXT one request may carry, in characters. ~6 K tokens, which is how much a
#: batch may add to a request that would have been made anyway — and that number is the width of the
#: promise a token Ceiling makes.
#:
#: The meter reserves a request's estimate before issuing it, so "a run overshoots its ceiling by at
#: most approximately one request's cost" is now one BATCH's cost, and a ceiling narrower than one
#: batch refuses the run at the gate having issued nothing. Two things keep that honest.
#: :func:`~seqforge.harvest.meter.estimated_tokens` reads the prompt actually being sent, so a batch
#: of three 200-character records estimates what a single such request estimates and coarsens nothing;
#: and this cap bounds the worst case at +6 K tokens over one document's request, which is smaller
#: than one long paper already is. Against the 500 K per-case ceiling `eval run` defaults to, a full
#: batch is ~1.7 % — well under, which is the property to preserve if this number ever moves.
#:
#: A document longer than the budget is never split: it is its own request. Splitting one would change
#: the text a quote is greppable against, which is the one thing this pipeline may not do.
MAX_BATCH_CHARS = 24_000

#: The output tokens one request may spend, reasoning included — the budget every width here is
#: divided out of. What it is *not* is a provider ceiling, and that distinction is the whole reason
#: the number is allowed to be round.
#:
#: **Measured, not read off a docs page.** Both V4 models were probed with a deliberate over-ask — a
#: ``drafts`` array of 600 objects, ~37 K output tokens, more than the budget can hold — so that what
#: stopped generation was the cap rather than the model deciding it was done. Both honoured
#: ``max_tokens`` to the token: ``finish_reason: length``, ``completion_tokens: 32000``, and none of
#: the silent clamp at 8192 or 16384 that would have made every width below a fiction. Both publish a
#: 384 K maximum output, so this sits ~12x under what the provider would allow.
#:
#: **It is priced below that maximum on purpose, and the reason is blast radius rather than the
#: ceiling being where it is.** A batch-level failure costs a re-ask of every member — the second
#: reason this module ever gave for a count cap, and the one that SURVIVES width becoming a function
#: of the ask, because the ``max_tokens`` reason does not. Held here, a dropped request re-asks at
#: most one budget's worth of drafts *whatever* the ask was, which is the honest unit: a count cap
#: was only ever a proxy for this, and a proxy calibrated against one ask. A 32 K-output request is
#: also a long request — 158 s and 247 s on the two successful measured calls, with a third attempt
#: dying at 181 s on a transient connection error the shipped retry covers — so widening this
#: multiplies both the wall clock and what one drop costs, for drafts that are almost never emitted.
#:
#: **And a second, independent reason to keep it well under the ceiling: it is what the Ceiling
#: overshoots by.** :class:`~seqforge.harvest.meter.TokenMeter` reserves the INPUT half of a request
#: and declines to reserve ``max_tokens`` outright, on the stated grounds that "the cap is several
#: times a real extraction's output" and reserving it would refuse runs that would have fitted. Its
#: own docstring concedes the consequence: during the first wave, before any exchange has banked, the
#: unreserved remainder is the output of every request in flight. Raising the per-request cap from
#: 8 000 to ~31 690 widens that window by ~4x. That trade is still the right one — but the multiple
#: moved, so anyone tightening a Ceiling should read this number rather than the old literal.
#:
#: If a provider ever does refuse an output ceiling this size, the fallback is a count cap PER ASK
#: WIDTH — 8 for the nine-field ask, 36 for the two-field one — rather than the single cap that used
#: to be here. The measurement says it is not needed.
BATCH_OUTPUT_BUDGET = 32_000

#: Reserved off the top of :data:`BATCH_OUTPUT_BUDGET` before any draft is budgeted for. Reasoning
#: tokens bill against the same ``max_tokens``, so a budget that assumed the whole ceiling reached the
#: drafts would be spending tokens the model had already taken.
#:
#: **Fixed, not a proportion, because the overhead is closer to constant than proportional** —
#: measured at 589 of 32 000 on a wide ask (1.8 %) and 70 of 114 on a trivial one (61 %). A
#: proportional reserve is negligible exactly where reasoning is negligible and negligible exactly
#: where it dominates, which leaves the *narrowest* asks — the ones deriving width from the ask
#: exists to un-throttle — the place the estimate is worst. That is backwards, and it is the failure
#: this constant exists to prevent.
#:
#: 1 000 rather than the 589 that was measured, because two calls are not a distribution. Being wrong
#: in the other direction costs a truncated response, which is a re-ask of the whole batch.
#:
#: **Calibrated on the one provider that was measured, and it is not a bound on every provider.**
#: On DeepSeek V4 there is nothing to bound: how much a V4 reasons is baked into ``-flash`` versus
#: ``-pro`` and the API takes no toggle. But :class:`~seqforge.harvest.providers.AnthropicProvider`
#: sends ``thinking={"type": "adaptive"}``, which is a toggle, and adaptive thinking bills against
#: this same ``max_tokens`` with no bound we set — so a reasoning-heavy turn there can spend past this
#: reserve. ADR-0009 makes the provider pluggable, so state the limit rather than imply generality.
#: What it costs when it happens is a truncated response, which is a batch-level failure
#: :func:`extract_planned` already re-asks one document at a time without losing a claim.
REASONING_HEADROOM_TOKENS = 1_000

#: What one draft costs to serialise, in output tokens — the rate a width is metered at.
#:
#: **The measured rate, carried unrounded, and that is the whole discipline here.** One draft under
#: the strict schema is 251 characters — 64 of them the echoed ``doc_sha256`` — which is ~62 tokens at
#: the meter's rule of thumb (:data:`CHARS_PER_TOKEN`). The same 62 is what makes the two numbers this
#: change exists to reconcile: the nine-field ask costs 558 tokens per document and the two-field ask
#: 124, so a fixed ``max_tokens`` of 8 000 had room for 14 of the former and **64** of the latter
#: against a cap of 8.
#:
#: **Rounding it down to 60 is the one thing this must not do**, and it was tried. 60 recovers the
#: width 57 that #233 decision 1 quotes — but decision 1 computed 57 as ``32000 / 558`` with **no
#: reserve at all**, before #282 required one. So shaving the rate hands straight back the headroom
#: :data:`REASONING_HEADROOM_TOKENS` had just taken, and the reserve becomes decorative. It is worse
#: than decorative: at width 57 the worst case is ``57 x 558 = 31 806`` output tokens against the
#: ``31 780`` such a request would ask for, so the batch overruns before reasoning has spent a token.
#: A constant chosen to match a projection, against a derivation that projection predates.
#:
#: Carried at 62 the width falls out at 55, and the two constants divide the labour honestly: this one
#: is the measurement, and the headroom absorbs the approximation. The cost is ~80 requests on the
#: 1440-record deposit where the projection said ~78 — inside #282's "~78", and correct by derivation
#: rather than by agreeing with a number computed under different assumptions.
#:
#: The rate still multiplies a WORST case — every asked field answered by every document — that the
#: prompt's own instruction 5 ("an empty ``drafts`` list is a CORRECT and common answer") says is
#: rare; and an overrun is a truncated response, which is a batch-level failure
#: :func:`extract_planned` already re-asks one document at a time without losing a claim. Nothing here
#: promises a draft fits in 62 tokens.
PER_DRAFT_TOKENS = 62


@dataclass(frozen=True)
class ExtractionPlan:
    """What will be asked, of what, and roughly what it costs — before a token is spent.

    The documents are already rendered, because rendering is free. That is what makes a dry run
    exact: it is the same list the paid run sends, not a projection of one.
    """

    #: In send order, which is also the order :func:`extract_planned` returns outcomes in.
    documents: tuple[NormalizedDoc, ...]
    #: ``doc_sha256`` -> the archive records folded into that document. One entry for a record
    #: rendered on its own; many for a collapsed run document; absent for a document a human handed us.
    members: dict[str, tuple[str, ...]]
    #: Archive records with prose that this plan reads. Records at a level with an empty ask
    #: (``project``) and records with no prose at all are not read, and are not counted here.
    n_records_read: int = 0
    #: Records this plan folded away: read, but not costing an exchange of their own.
    n_records_collapsed: int = 0
    #: The stable system prefix, in characters. It is byte-identical on every request — which is what
    #: makes prefix caching work — and it is therefore paid once **per document**, not once per run.
    system_prompt_chars: int = 0

    @property
    def n_documents(self) -> int:
        return len(self.documents)

    @property
    def batches(self) -> tuple[tuple[int, ...], ...]:
        """Indices into :attr:`documents`, grouped into the requests they will be sent as."""
        return batch_documents(self.documents)

    @property
    def n_requests(self) -> int:
        """How many times this plan will reach a model, before any retry. Never more than
        :attr:`n_documents`, and fewer whenever two documents receive the same ask."""
        return len(self.batches)

    @property
    def n_chars(self) -> int:
        """Document text this plan will send, in characters. The volatile half of the prompt."""
        return sum(len(d.text) for d in self.documents)

    @property
    def estimated_input_tokens(self) -> int:
        """A plan's whole point: the system prefix is charged **per request**.

        It used to be charged per document, and that was the same number until documents began
        sharing a request. Keeping it per document would leave a dry run overstating a real run by
        ``(n_documents - n_requests) x system_prompt_chars``, which on a dataset of one-line archive
        records is most of the bill — a dry run whose whole job is to be the price of the paid run
        must move when the paid run does, in both directions.

        Output is not estimated and is not estimable — the model decides how many claims a document
        supports. The token Ceiling is what bounds that half; this bounds the half we choose.

        ``CHARS_PER_TOKEN`` comes from the meter rather than living here, because the meter applies
        the same rule of thumb per request: it reserves a request's estimated cost against the
        Ceiling before issuing it. A plan costed by a second constant could fit under a ceiling that
        then refused it, and a dry run that disagrees with the run it is a dry run of is worse than
        no dry run.
        """
        return (self.n_requests * self.system_prompt_chars + self.n_chars) // CHARS_PER_TOKEN

    def asked(self, doc: NormalizedDoc) -> tuple[str, ...]:
        """The fields this document will be asked for. Extraction derives the same set."""
        return fields_for(doc.scope, doc.role)

    def report(self) -> ExtractionPlanReport:
        """The wire form: what a ``--dry-run`` prints, and the first-class result type it needs."""
        return ExtractionPlanReport(
            n_documents=self.n_documents,
            n_requests=self.n_requests,
            n_records_read=self.n_records_read,
            n_records_collapsed=self.n_records_collapsed,
            n_chars=self.n_chars,
            system_prompt_chars=self.system_prompt_chars,
            estimated_input_tokens=self.estimated_input_tokens,
            documents=[
                PlannedDocument(
                    doc_sha256=d.doc_sha256,
                    source=d.source_basename,
                    role=d.role,
                    scope=d.scope,
                    subject=d.subject,
                    n_chars=len(d.text),
                    fields=list(self.asked(d)),
                    members=list(self.members.get(d.doc_sha256, ())),
                )
                for d in self.documents
            ],
        )


def plan_extraction(
    *,
    documents: Sequence[NormalizedDoc] = (),
    records: ArchiveRecordSet | None = None,
    system_prompt_chars: int = 0,
) -> ExtractionPlan:
    """Documents a human handed us, plus the records worth asking about, as ONE send list.

    ``documents`` arrive already normalized, because their role came from the flag they arrived under
    and only the CLI knows that. Records are rendered here: which of them are worth a call, and how
    many calls they are worth, is exactly the decision this module owns.

    ``system_prompt_chars`` is the stable prefix's length, for the cost estimate only. It is passed in
    rather than built here so that planning stays free of the KB load and of a provider's schema.
    """
    planned: list[NormalizedDoc] = list(documents)
    members: dict[str, tuple[str, ...]] = {}
    n_read = 0
    n_collapsed = 0

    if records is not None:
        for record in records.records:
            if record.level == "run" or not _worth_asking(record):
                continue
            n_read += 1
            doc = normalize_record(record)
            planned.append(doc)
            members[doc.doc_sha256] = (record.accession,)
        for owner, group in _runs_by_sample(records):
            n_read += len(group)
            n_collapsed += len(group) - 1
            doc = _collapsed_run_document(owner, group)
            planned.append(doc)
            members[doc.doc_sha256] = tuple(r.accession for r in group)

    return ExtractionPlan(
        documents=tuple(_deduplicated(planned)),
        members=members,
        n_records_read=n_read,
        n_records_collapsed=n_collapsed,
        system_prompt_chars=system_prompt_chars,
    )


def batch_width(fields: Sequence[str]) -> int:
    """The most documents one request may carry, given what that request will ask them.

    ``(BATCH_OUTPUT_BUDGET - REASONING_HEADROOM_TOKENS) / (n_asked x PER_DRAFT_TOKENS)``, over the
    ask itself — which is already :func:`batch_documents`' group key, so nothing new has to be
    decided to know a batch's width.

    **This was one number for every ask, and that is the defect.** A fixed cap of eight was
    worst-case-tuned against the widest (nine-field) ask and then applied to the narrowest
    (two-field), where the same fixed ``max_tokens`` had room for 64 — so every narrow-ask batch has
    been throttled ~8x on every dataset since batching landed. The saving is not the argument.
    ``8`` and ``8000`` were two constants that only made sense together and were never tuned against
    each other; either alone was defensible and jointly they left the narrow ask throttled forever.
    Deriving one from the other is what stops that recurring.

    Rejected: **raise the cap from 8 to 14**, which is worst-case-exact against a fixed 8 000 output
    ceiling and arithmetically correct. It leaves the two-field ask 4.5x under-batched, and it
    re-creates precisely the coupling above — a second pair of numbers, hand-fitted to one ask, that
    the next change to either would silently invalidate.

    Two bounds this deliberately does not have. There is no absolute count cap beside it: the budget
    bounds a dropped request's cost in OUTPUT TOKENS, uniformly across asks, which is what a count cap
    was approximating (see :data:`BATCH_OUTPUT_BUDGET`). And there is no input term:
    :data:`MAX_BATCH_CHARS` is the input bound and still binds first for anything long — the
    experiment-scope ask comes out at 250 here and hits the character budget at ~55.

    Never below one, and total on an empty ask. A ``project`` document is asked nothing and is never
    planned (:func:`_worth_asking` drops it), but this is a public function over arbitrary documents
    and a divisor of zero is not a width — an empty ask is charged as one field rather than special-
    cased, because a request that can produce no draft is bounded by its characters alone anyway.
    """
    per_document = max(1, len(fields)) * PER_DRAFT_TOKENS
    return max(1, (BATCH_OUTPUT_BUDGET - REASONING_HEADROOM_TOKENS) // per_document)


def batch_max_tokens(fields: Sequence[str], n_documents: int) -> int:
    """The output ceiling one request asks for: the reasoning headroom, plus what its drafts can cost.

    The inverse of :func:`batch_width`, and it has to be — a width derived from a budget that the
    request then never asked for would be a plan for a request nobody sends. So the same three
    constants appear on both sides, and a full-width batch asks for very nearly the whole budget.

    Bounded at both ends, and both ends are load-bearing:

    - never above :data:`BATCH_OUTPUT_BUDGET`, which is what makes the budget a bound on a REQUEST
      and not merely on a width. Unreachable from :func:`batch_documents`, whose widths are derived
      from that budget; a caller batching by hand still cannot ask for more than one was priced at.
    - never below :data:`~seqforge.harvest.extract.DEFAULT_MAX_OUTPUT_TOKENS`, the ceiling every
      extraction has been made under until now. A width-one request already renders byte-identically
      to a pre-batching one (:func:`~seqforge.harvest.extract._batch_user_content`), and the floor
      extends that to the parameters: one document asks for exactly what it asked for before, so
      nothing this change does can truncate a document that used to fit. That matters most on the
      path with the least to gain — :func:`extract_planned`'s per-document fallback, which exists to
      recover a batch that failed and would be no fallback at all if it could fail by truncation
      where the batch did not.
    """
    drafts = n_documents * len(fields) * PER_DRAFT_TOKENS
    return min(
        BATCH_OUTPUT_BUDGET, max(DEFAULT_MAX_OUTPUT_TOKENS, REASONING_HEADROOM_TOKENS + drafts)
    )


def batch_documents(documents: Sequence[NormalizedDoc]) -> tuple[tuple[int, ...], ...]:
    """Plan-ordered document indices, grouped into the requests they will be sent as.

    **The group key is the ask itself** — ``fields_for(scope, role)`` — and not the ``(scope, role)``
    pair it is derived from. A prompt is the only place the ask exists, so two documents may share a
    request exactly when the request would put the same question to both; anything else asks one of
    them for fields it will never be permitted to answer, which wastes the ask on every request. That
    the key is the ask and not its inputs is worth the indirection: a ``sample`` record and a
    collapsed ``run`` document are asked the same nine attributes, and keying on scope would send two
    requests to ask one question. Safety is not what this decides — ``verify_drafts`` refuses an
    off-scope field whatever was asked.

    Two caps bound a request and they bound different halves of it. :data:`MAX_BATCH_CHARS` bounds
    the INPUT, and it still binds first for anything long. :func:`batch_width` bounds the OUTPUT, and
    it is a function of that same group key — the ask decides both who may share a request and how
    many may, which is the one thing that keeps the width from being a constant tuned against a
    different ask than the one being sent. A batch also never holds one document's sha256 twice: the
    model routes its drafts by that sha, and two members it cannot tell apart is a question with no
    answer. A document that alone exceeds the character budget is its own request rather than being
    split.

    Batches come back ordered by their first member and each is ascending, so a plan's requests are a
    deterministic function of the plan — the same documents produce the same requests on a laptop and
    on a 48-core node. Nothing here reads a core count, a clock or an environment:
    :data:`MAX_IN_FLIGHT` sizes the POOL that carries these batches, never the batches themselves.
    """
    open_batches: dict[tuple[str, ...], list[int]] = {}
    open_chars: dict[tuple[str, ...], int] = {}
    closed: list[list[int]] = []

    for i, doc in enumerate(documents):
        key = fields_for(doc.scope, doc.role)
        current = open_batches.get(key)
        if current is not None and (
            len(current) >= batch_width(key)
            or open_chars[key] + len(doc.text) > MAX_BATCH_CHARS
            or any(documents[j].doc_sha256 == doc.doc_sha256 for j in current)
        ):
            closed.append(current)
            current = None
        if current is None:
            current = open_batches[key] = []
            open_chars[key] = 0
        current.append(i)
        open_chars[key] += len(doc.text)

    closed.extend(open_batches.values())
    return tuple(tuple(b) for b in sorted(closed, key=lambda b: b[0]))


def extract_planned(
    plan: ExtractionPlan,
    specs: dict[str, Spec],
    *,
    provider: LLMProvider,
    model: str | None = None,
    partial: bool = False,
) -> list[ExtractionOutcome]:
    """Extract every planned document, concurrently, returned **in plan order**.

    A LIST, positionally aligned with ``plan.documents``, and that is the whole reassembly contract:
    a dict keyed by document identity loses one of two identical documents, and a completion-ordered
    list would make a run's graded assertions depend on which socket returned first.

    **One request per batch, not per document** (:func:`batch_documents`), and each request is an
    independent, network-bound call, so they go out on a THREAD pool — the wait is on a socket, and
    processes would only add IPC. :data:`_SLOTS` is what the provider actually sees, because a caller
    may already be inside a pool of its own.

    **Each request asks for the output ceiling its own batch was priced at**
    (:func:`batch_max_tokens`), rather than the ceiling being pinned per call site. A width divided
    out of a budget the sender then never asked for would leave every full batch truncating at a
    number nobody chose. It is a pure function of the batch's ask and its width — both plan-time — so
    the dry run and the paid run cannot disagree about it.

    **A batch-level failure falls back to per-document calls, immediately.** Unbatched, a failed
    request costs one document; batched it could cost the whole batch — 55 of them at the
    nine-attribute ask — which would make "half a batch is worse than none" worse rather than better.
    So batching may never lose more documents than not batching would have: worst case one extra
    round trip, best case most of the per-request prompt overhead gone. Three properties make that a
    guarantee rather than a hope —

    - the fallback re-asks through a request shape that has never been batched (one document, and the
      echoed sha discarded), so it cannot fail for the reason the batch did;
    - the retries run SEQUENTIALLY inside the slot the batch already holds. A nested pool reaching for
      :data:`_SLOTS` while holding one of them deadlocks as soon as every slot is held by a batch
      waiting on a retry;
    - what the fallback then does with a document that fails alone is exactly what an unbatched run
      does, ``partial`` and all.

    ``partial`` decides what one document's failure costs the other twelve, and the two callers want
    opposite answers. **Off (the default) it raises**, so the compiler fails closed: an extraction
    missing a document produces a manifest that is silently short a fact, and there is no way to tell
    that manifest from a complete one afterwards. **On, an unanswered document comes back as an
    outcome carrying its** ``failure`` — because a harness measuring the stage learns nothing from
    twelve documents it declined to send, and a whole dataset raising on its one flaky supplementary
    table is how a graded tier reported nothing at all about five of its seven prose cases.

    A **Ceiling** is not caught either way, and a batch failure does not fall back past one. It is a
    refusal rather than an unavailability — the provider answered everything it was given — and the
    caller owes it a ``Blocker``, so it must still reach one. The meter reconciles a reservation on
    every path out of a request, so a batch that failed has already given its estimate back by the
    time the retries ask for theirs; what the retries do face is the batch's REAL spend, which was
    banked because it was really spent.
    """

    def _alone(doc: NormalizedDoc) -> ExtractionOutcome:
        """One document, one request — what an unbatched run does, and what a fallback retries as.

        Priced at width one off this document's own ask, so the fallback is a coherent request rather
        than one carrying whatever ceiling the batch it is recovering from happened to ask for.
        """
        max_tokens = batch_max_tokens(plan.asked(doc), 1)
        if not partial:
            return extract_drafts(doc, specs, provider=provider, model=model, max_tokens=max_tokens)
        try:
            return extract_drafts(doc, specs, provider=provider, model=model, max_tokens=max_tokens)
        except ExtractUnavailable as exc:
            return ExtractionOutcome.unanswered(
                provider=provider.name,
                model=model or provider.default_model(),
                detail=str(exc),
            )

    def _one(batch: tuple[int, ...]) -> list[ExtractionOutcome]:
        docs = [plan.documents[i] for i in batch]
        with _SLOTS:
            if len(docs) == 1:
                return [_alone(docs[0])]
            try:
                return extract_batch(
                    docs,
                    specs,
                    provider=provider,
                    model=model,
                    max_tokens=batch_max_tokens(plan.asked(docs[0]), len(docs)),
                )
            except ExtractUnavailable:
                # The whole of the decision: this batch is unusable, so ask its documents one at a
                # time. The failure itself is not reported — every document is about to be asked
                # again, and one that answers alone was never lost to report.
                return [_alone(d) for d in docs]

    batches = plan.batches
    if len(batches) <= 1:
        answered = [_one(b) for b in batches]
    else:
        with ThreadPoolExecutor(max_workers=min(MAX_IN_FLIGHT, len(batches))) as pool:
            answered = list(pool.map(_one, batches))

    # Scattered back to plan positions rather than concatenated: a batch is a set of positions, and
    # only the batching happens to be order-preserving today.
    ordered: dict[int, ExtractionOutcome] = {}
    for batch, outcomes in zip(batches, answered, strict=True):
        ordered.update(zip(batch, outcomes, strict=True))
    return [ordered[i] for i in range(len(plan.documents))]


def _worth_asking(record: ArchiveRecord) -> bool:
    """Prose to read, and something to ask of a record at this level.

    ``project`` is asked nothing at all, so a project record costs a call to be told nothing; a record
    with no free text has nothing for a model to read. Both are dropped before anything is rendered.
    """
    return has_prose(record) and bool(fields_for(record.level, "reference"))


def _runs_by_sample(records: ArchiveRecordSet) -> list[tuple[str, list[ArchiveRecord]]]:
    """The run records worth asking, grouped by the sample each belongs to.

    The group key is the sample's accession where the join reaches one, and the run's own accession
    where it does not — a run whose sample record is absent still has an identity, and folding it in
    with unrelated runs would be inventing one.
    """
    groups: dict[str, list[ArchiveRecord]] = {}
    for run in records.at("run"):
        if not _worth_asking(run):
            continue
        sample = records.ancestor(run, "sample")
        groups.setdefault(sample.accession if sample is not None else run.accession, []).append(run)
    return [(key, groups[key]) for key in sorted(groups)]


def _collapsed_run_document(owner: str, runs: Sequence[ArchiveRecord]) -> NormalizedDoc:
    """Every run of one sample, as ONE document that still names that sample.

    ``subject`` is the **sample's** accession rather than any one run's, and that placement is the
    load-bearing part: :func:`seqforge.resolve.records._basis_for` keeps a claim only when the
    document's subject maps to the sample being resolved, and ``_subject_to_sample`` maps a sample
    accession to itself. Point this at one of the runs instead and the other eleven aliases in the
    same document would be citing a run they did not come from; point it anywhere else and every
    claim from the document is silently discarded.

    ``scope`` stays ``run`` because that is what the prose IS, and the ask follows the scope: a run
    document is asked the sample-attribute vocabulary, which is precisely what an alias answers. The
    text is the members' own renderings concatenated in accession order, so it stays reproducible
    from the record set forever — a quote is only checkable while the exact bytes can be regenerated.

    Every member's typed columns are marked in the joined text, not in its own rendering
    (:func:`~seqforge.harvest.normalize.declared_spans`): the join is the only string a quote is ever
    checked against, so an offset into anything else would point at the wrong characters.
    """
    ordered = sorted(runs, key=lambda r: r.accession)
    if len(ordered) == 1:
        return normalize_record(ordered[0])
    text = normalize_text("\n\n".join(render_record(r) for r in ordered))
    digest = hashlib.sha256(text.encode()).hexdigest()
    scope: DocScope = "run"
    return NormalizedDoc(
        doc_sha256=digest,
        normalized_sha256=digest,
        text=text,
        source_basename=f"runs-{owner}.txt",
        role="reference",
        scope=scope,
        subject=owner,
        n_chars=len(text),
        declared=declared_spans(text, ordered),
    )


def _deduplicated(documents: Sequence[NormalizedDoc]) -> list[NormalizedDoc]:
    """Drop a document whose ask is byte-identical to one already in the list.

    Identity here is the whole question the call would put — the text, plus the role and scope that
    decide which fields are asked of it. The same PDF passed twice as a reference is one call; passed
    once as a reference and once under ``--instruction`` it is two, because those are two different
    asks and one of them may reach ``processing.*``.
    """
    seen: set[tuple[str, str, str, str | None]] = set()
    kept: list[NormalizedDoc] = []
    for doc in documents:
        key = (doc.doc_sha256, doc.role, doc.scope, doc.subject)
        if key in seen:
            continue
        seen.add(key)
        kept.append(doc)
    return kept


__all__ = [
    "BATCH_OUTPUT_BUDGET",
    "CHARS_PER_TOKEN",
    "MAX_BATCH_CHARS",
    "MAX_IN_FLIGHT",
    "PER_DRAFT_TOKENS",
    "REASONING_HEADROOM_TOKENS",
    "ExtractionPlan",
    "batch_documents",
    "batch_max_tokens",
    "batch_width",
    "extract_planned",
    "plan_extraction",
]
