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

**Records that SAY the same thing are one ask, and the rest of them are their own difference.**
``_deduplicated`` keys on the whole text, so 1440 sample records differing only in an accession —
semantically identical, lexically distinct — slip past it entirely. :func:`_collapse_near_identical`
is what sees them: one exemplar carries the group's prose in full with its variants MARKED, every
other member is sent as its **distinctive bytes only**, and a member whose distinctive bytes are
nothing but its own accession is not sent at all. :func:`fan_claims` then extends the exemplar's
claims to every member whose own bytes carry the quote.

The guarantee is **no unread byte**, never *no wrong claim*: the invariant is read once and every
other member's difference is read. Why the cheaper reading — fan a claim out and send nobody — is
rejected, with the pilot that falsified it, is argued once in ADR-0031. Measured on the 1440-record
GSE207085 dump, and measured on top of the width rule above rather than instead of it: **786 906
characters over 80 requests become 194 038 over 59**, ~375 K estimated input tokens becoming ~180 K.
No record goes unread — every level carries a per-cell serial name (``nasal_prox1_270``,
``GSM6277169_r1``), so nothing is withheld and 1439 of each 1440 are asked their difference and
nothing else.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from ..models.assertion import Assertion, ExtractionPlanReport, PlannedDocument, SourceSpan
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
    VariantSpan,
    declared_spans,
    has_prose,
    is_invariant_span,
    normalize_record,
    normalize_text,
    page_for_offset,
    render_record,
    token_skeleton,
    token_spans,
    token_values,
    variant_spans,
    variant_text,
    varying_token_indices,
)
from .verify import find_span

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

#: The nine manifest paths whose subject is ONE sample. They decide the fan-out UNIT, and the field
#: path is what says which (#233 decision 5): a sample-scoped claim is materialized once per member,
#: each citing that member's own document, while a dataset-scoped one stays a single Assertion because
#: ``chemistry_hypothesis`` reduces N identical claims to one regardless — what the grep buys there is
#: the *proof of unanimity*, not a claim per record.
#:
#: Read out of :mod:`seqforge.harvest.fields` rather than spelled again here: a second list of the
#: nine is a second vocabulary, and the two would drift the first time an attribute is added.
_SAMPLE_SCOPED_FIELDS = frozenset(fields_for("sample", "reference"))


@dataclass(frozen=True)
class CollapsedGroup:
    """The near-identical records one exemplar was read FOR, split by what each of them cost.

    Both halves hold **full renderings** — the bytes
    :func:`~seqforge.harvest.normalize.render_record` produced, never the reduced text — because that
    is what a fanned Assertion cites and what must reach disk (ADR-0031). Splitting them is not
    bookkeeping. It is the difference between a record this plan reads only through somebody else's
    prose and a record this plan asks in its own right, and a reader handed the union alone would
    take a 1440-member exemplar for 1439 records that went unread — the exact opposite of the
    guarantee the collapse is built on.

    So a :attr:`reduced` member appears in a plan twice: here, as the full rendering a fanned claim
    cites, and in :attr:`ExtractionPlan.documents` as the short document carrying its distinctive
    bytes. A :attr:`withheld` member appears once, and never as something sent.
    """

    #: Members whose distinctive bytes ARE sent, as a document of their own.
    reduced: tuple[NormalizedDoc, ...] = ()
    #: Members not sent at all: their distinctive tokens were nothing but the accession we wrote.
    withheld: tuple[NormalizedDoc, ...] = ()

    @property
    def members(self) -> tuple[NormalizedDoc, ...]:
        """Every member the exemplar stands for, whatever it cost — which is exactly the set its
        invariant claims fan to, because a fan is decided by bytes and never by price."""
        return self.reduced + self.withheld


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
    #: Records this plan folded away: read, but not costing a document of their own — the runs of one
    #: sample, and a near-identical member whose only difference is the accession we ourselves wrote.
    n_records_collapsed: int = 0
    #: Records sent as their DISTINCTIVE BYTES only: their difference costs a document, the invariant
    #: they share does not. Counted apart from ``n_records_collapsed`` because they are not the same
    #: fact — one is a record that cost nothing, the other a record that cost what it is worth.
    n_records_reduced: int = 0
    #: The stable system prefix, in characters. It is byte-identical on every request — which is what
    #: makes prefix caching work — and it is therefore paid once **per document**, not once per run.
    system_prompt_chars: int = 0
    #: Exemplar ``doc_sha256`` -> the group of near-identical records whose prose it carries. Their
    #: full renderings are never sent — a reduced member's *difference* is, and a withheld member's
    #: nothing — but they are still RENDERED, because rendering is free and because a fanned Assertion
    #: *cites* one of them: a span citation is checkable only while the exact text survives, so
    #: ``harvest extract`` writes these to ``documents/`` beside the ones it paid for and names them in
    #: ``document_subjects`` — without which ``resolve`` would drop every fanned claim for having no
    #: subject (ADR-0031).
    collapsed: dict[str, CollapsedGroup] = field(default_factory=dict)

    @property
    def n_documents(self) -> int:
        return len(self.documents)

    @property
    def all_documents(self) -> tuple[NormalizedDoc, ...]:
        """Every document this plan RENDERED — the ones it sends, each followed by the full
        renderings of the ones it collapsed onto it. What must reach disk, as against
        :attr:`documents`, which is what is paid for."""
        out: list[NormalizedDoc] = []
        for doc in self.documents:
            out.append(doc)
            group = self.collapsed.get(doc.doc_sha256)
            if group is not None:
                out.extend(group.members)
        return tuple(out)

    def stands_for(self, doc: NormalizedDoc) -> tuple[str, ...]:
        """Every archive record ``doc`` is the ONLY reading of: its own, the runs of one sample folded
        into it, and every near-identical member **withheld** onto it — the ones whose difference was
        nothing but the accession we ourselves wrote, so nothing of theirs was sent.

        A member the collapse **reduced** is deliberately absent; it is in :meth:`reduced_members`,
        because its difference *was* sent, as a document of its own. Across a whole plan every record
        read appears in exactly one document's ``stands_for``, which is the arithmetic that makes "no
        record went unread" checkable rather than merely asserted. Why either number is reported at
        all, given that neither moves the epistemics, is argued once on
        :class:`~seqforge.models.assertion.PlannedDocument`.
        """
        group = self.collapsed.get(doc.doc_sha256)
        withheld = group.withheld if group is not None else ()
        return self.members.get(doc.doc_sha256, ()) + self._accessions(withheld)

    def reduced_members(self, doc: NormalizedDoc) -> tuple[str, ...]:
        """Every near-identical record that shares ``doc``'s prose and was sent its own DIFFERENCE.

        They cost a document each — their distinctive bytes, further down :attr:`documents` — and it
        is there that they are read; what ``doc`` bought them is the invariant, read once. A claim of
        ``doc``'s that touches no variant fans to them exactly as it does to :meth:`stands_for`'s,
        because a fan is decided by bytes and never by price.
        """
        group = self.collapsed.get(doc.doc_sha256)
        return self._accessions(group.reduced) if group is not None else ()

    def _accessions(self, documents: Iterable[NormalizedDoc]) -> tuple[str, ...]:
        return tuple(a for d in documents for a in self.members.get(d.doc_sha256, ()))

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
            n_records_reduced=self.n_records_reduced,
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
                    members=list(self.stands_for(d)),
                    reduced_members=list(self.reduced_members(d)),
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

    **The near-identical collapse happens HERE and never at send time.** This function's whole claim is
    that the dry run is the same list the paid run sends rather than a projection of one; collapse in
    ``extract_planned`` and both ``harvest extract --dry-run`` and ``eval plan`` report a bill nobody
    pays, which is the one thing this module exists to prevent.
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

    collapse = _collapse_near_identical(_deduplicated(planned), members)
    # The fold hands its new entries back rather than writing them into `members` behind us: a
    # reduced document is a document nothing else knew about, and its record has to be findable
    # under its sha or `stands_for` cannot name it.
    members.update(collapse.members)

    return ExtractionPlan(
        documents=collapse.documents,
        members=members,
        n_records_read=n_read,
        n_records_collapsed=n_collapsed + collapse.n_records_withheld,
        n_records_reduced=collapse.n_records_reduced,
        system_prompt_chars=system_prompt_chars,
        collapsed=collapse.groups,
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


@dataclass(frozen=True)
class Collapse:
    """What :func:`_collapse_near_identical` decided — named fields, never a positional tuple.

    :attr:`members` is the reason this is a type rather than a fourth tuple element. The fold used to
    write its new entries straight into the caller's ``members`` map — a fourth rule, stated nowhere
    among the three its docstring gives and expected by no reader of them. Handed back, the edit
    becomes the caller's, and it is visible beside the map it changes.
    """

    #: The send list: every document that survives the fold, in plan order.
    documents: tuple[NormalizedDoc, ...]
    #: Exemplar ``doc_sha256`` -> the group of near-identical records it carries the prose of.
    groups: dict[str, CollapsedGroup]
    #: New ``members`` entries, one per reduced document, each naming the record its bytes came from.
    members: dict[str, tuple[str, ...]]
    #: Records folded away entirely, and records sent as their difference — counted in RECORDS, not
    #: in documents, which is why they are computed here: only the fold holds both maps at once.
    n_records_withheld: int
    n_records_reduced: int


def _collapse_near_identical(
    documents: Sequence[NormalizedDoc], members: Mapping[str, tuple[str, ...]]
) -> Collapse:
    """Fold records that say the same thing onto one exemplar, under **no unread byte**.

    1440 sample records that differ only in an accession are semantically identical and *lexically
    distinct*, so :func:`_deduplicated` — which keys on the whole text — misses them entirely. The
    fold is three rules and no threshold:

    - **Group** the record-derived documents by ``(scope, role, token_skeleton)``. One skeleton means
      one punctuation and one token count, so the members differ only in what their tokens SAY. A
      record whose paragraph is shaped differently simply forms its own group and is asked on its own.
    - **Send** every other member as its **distinctive bytes only**
      (:func:`~seqforge.harvest.normalize.variant_text`) — the invariant it shares was read once, in
      the exemplar, and 1439 more copies of one paragraph is the bill this exists to stop.
    - **Withhold** entirely the member whose distinctive tokens are *all* its own accession
      (:func:`_nothing_to_ask`): its reduced document would say nothing but its own name.

    Three outcomes, one per member, and the middle one is what delivers the guarantee rather than
    merely claiming it: the invariant is read once and **every other member's distinctive bytes are
    read**. No unread byte. It is also why a plate whose wells really do differ prices itself — a
    record that says `day7` where 1439 say `day3` has `day7` in its variant document and is asked.

    **Mark, never splice — and that governs the EXEMPLAR.** The document carrying the group's prose
    is one member's own rendering with the variants marked, never a synthesised concatenation of
    spans gathered from across the group: a model must read coherent prose, and a quote into a
    stitched string could be checked against nothing. It was never a claim about the other members,
    whose distinctive bytes are exactly what decision 3 says to ask.

    **The exemplar is the first member that carries something askable**, so the invariant rides on a
    document the plan was already sending; only where every member is nothing but its own accession
    does the collapse spend a document of its own. Documents a human handed us are never grouped:
    they have no record behind them, so there is no accession to recognize and nothing to fold onto.

    Rejected, and it must not be re-proposed: **fan-out-only** — fan a claim to every record whose
    bytes carry the quote and never send the others. Argued once, with the pilot that falsified it,
    under ADR-0031's "Why not skip the fan-out and simply not collapse".
    """
    groups: dict[tuple[str, str, tuple[str, ...]], list[int]] = {}
    for i, doc in enumerate(documents):
        if doc.doc_sha256 not in members:
            continue  # a paper is not a record: no accession to recognize, nothing to fold onto
        groups.setdefault((doc.scope, doc.role, token_skeleton(doc.text)), []).append(i)

    stood_for: dict[int, tuple[int, ...]] = {}
    marks: dict[int, tuple[VariantSpan, ...]] = {}
    #: member index -> the reduced document sent in its place, or ``None`` where it is withheld.
    instead: dict[int, NormalizedDoc | None] = {}

    for indices in groups.values():
        if len(indices) < 2:
            continue
        texts = [documents[i].text for i in indices]
        varying = varying_token_indices(texts)
        if not varying:
            continue  # byte-identical, which `_deduplicated` already handled
        askable = [
            not _nothing_to_ask(documents[i], members, token_values(documents[i].text), varying)
            for i in indices
        ]
        lead = next((p for p, ok in enumerate(askable) if ok), 0)
        exemplar = indices[lead]
        others = tuple(indices[p] for p in range(len(indices)) if p != lead)
        stood_for[exemplar] = others
        marks[exemplar] = variant_spans([texts[lead], *(documents[o].text for o in others)])
        for p, i in enumerate(indices):
            if p != lead:
                instead[i] = _variant_document(documents[i], varying) if askable[p] else None

    folded = {
        documents[exemplar].doc_sha256: CollapsedGroup(
            reduced=tuple(documents[o] for o in others if instead[o] is not None),
            withheld=tuple(documents[o] for o in others if instead[o] is None),
        )
        for exemplar, others in stood_for.items()
    }

    def _records(docs: Iterable[NormalizedDoc]) -> int:
        """Documents to RECORDS: a document may stand for several already (a sample's runs)."""
        return sum(len(members.get(d.doc_sha256, ())) for d in docs)

    sent: list[NormalizedDoc] = []
    added: dict[str, tuple[str, ...]] = {}
    for i, doc in enumerate(documents):
        if i in stood_for:
            # The marks travel on a COPY, and `doc_sha256` is untouched by them: the bytes sent are
            # still this record's own rendering, so the identity an Assertion cites is regenerable
            # from that record alone. Only which claims may fan changes.
            sent.append(replace(doc, variants=marks[i]))
        elif i in instead:
            reduced = instead[i]
            if reduced is not None:
                added[reduced.doc_sha256] = members[doc.doc_sha256]
                sent.append(reduced)
        else:
            sent.append(doc)
    return Collapse(
        documents=tuple(sent),
        groups=folded,
        members=added,
        n_records_withheld=sum(_records(g.withheld) for g in folded.values()),
        n_records_reduced=sum(_records(g.reduced) for g in folded.values()),
    )


def _variant_document(doc: NormalizedDoc, varying: Sequence[int]) -> NormalizedDoc:
    """One member's distinctive bytes, as its own document.

    Its ``subject`` and ``scope`` are the record's, so a claim from it names that record and nothing
    else — no marks, and nothing here ever fans, because there is no invariant in it to fan. The
    ``-variant`` in the basename is not decoration: this text is NOT
    :func:`~seqforge.harvest.normalize.render_record`'s output, and a reader who finds it beside the
    full rendering must be able to tell which is which (ADR-0031).
    """
    text = variant_text(doc.text, varying)
    digest = hashlib.sha256(text.encode()).hexdigest()
    return NormalizedDoc(
        doc_sha256=digest,
        normalized_sha256=digest,
        text=text,
        source_basename=f"{doc.scope}-{doc.subject}-variant.txt",
        role=doc.role,
        scope=doc.scope,
        subject=doc.subject,
        n_chars=len(text),
        # No `declared` marks: a typed column that survived the reduction is a token that VARIES, and
        # the reduction dropped the label that made it a column in the first place, so re-deriving
        # them against a text no record wrote would be marking a coincidence.
    )


def _nothing_to_ask(
    doc: NormalizedDoc,
    members: Mapping[str, tuple[str, ...]],
    values: Sequence[str],
    varying: Sequence[int],
) -> bool:
    """Do this member's distinctive tokens carry nothing a model could be asked about?

    Equivalently, and this is the sentence to keep: would its variant document
    (:func:`~seqforge.harvest.normalize.variant_text`) say nothing but its own name? One predicate,
    and it is the difference between *reduce* and *withhold*.

    True only when every token that distinguishes it from its group is **its own accession** — the
    record's identity, which is in the document because :func:`~seqforge.harvest.normalize.render_record`
    put it there, not because a submitter wrote it. Code already owns that string, no permitted field
    at a record scope can be entailed by it (:mod:`seqforge.harvest.fields` grants a record document
    the nine sample attributes, or the chemistry and ``treatment``), and it is the one variant that is
    provably not prose.

    **Rejected: also exempting a token byte-equal to a typed column** (a
    :class:`~seqforge.harvest.normalize.DeclaredSpan`), on the argument that ``verify`` refuses a quote
    lying wholly inside one anyway. It does — but only *wholly* inside: a quote reaching past the
    column survives on purpose, so ``treatment: DMSO`` is a real claim even where ``DMSO`` is also a
    column, and only a *sample* record's columns are read by ``resolve_metadata`` in the first place.
    Withholding on that test would lose a run's or an experiment's typed contrast entirely. The
    accession is the only exemption that costs nothing.
    """
    accessions = frozenset(members.get(doc.doc_sha256, ()))
    return all(values[k] in accessions for k in varying)


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


@dataclass(frozen=True)
class FannedClaim:
    """One verified claim whose quote proved byte-identical across the group it was read in.

    The **claim** side of the collapse's legibility, and the thing ``PlannedDocument``'s two member
    lists cannot answer: those say which records one document was read for, this says *this value*
    holds of N of them. Why a count is reported at all, given that N does not move the epistemics, is
    argued once on :class:`~seqforge.models.assertion.PlannedDocument`.
    """

    #: Every stored Assertion this claim produced: one for a dataset-scoped field, one per member for
    #: a sample-scoped one. The first is always the claim as the model made it.
    assertion_ids: tuple[str, ...]
    field: str
    value: str
    quote: str
    #: The document the model actually read.
    source_doc_sha256: str
    #: Every archive record whose own bytes carry this quote.
    records: tuple[str, ...]
    #: Did the fan produce an Assertion per record (sample-scoped), or prove unanimity (dataset)?
    materialized: bool

    @property
    def n_records(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class FanReport:
    """What survived verification, after a collapse's claims were fanned to the records they hold of."""

    assertions: list[Assertion]
    fanned: list[FannedClaim]


def fan_claims(assertions: Sequence[Assertion], plan: ExtractionPlan) -> FanReport:
    """Extend each verified claim to every record the collapse proved its quote greps into.

    **A claim fans out iff its quote lies entirely inside spans byte-identical across the group**
    (:func:`~seqforge.harvest.normalize.is_invariant_span`) — which greps into every member by
    construction, so this replaces N model judgements with N byte comparisons. It is what keeps
    ``chemistry_hypothesis``'s unanimity check ("agreement or nothing") from going vacuous under a
    collapse: a record whose paragraph says something else has a different skeleton or a different
    token, fails the grep, leaves the group, and gets its own ask.

    **The unit follows field arity, and the field path already says which** (#233 decision 5):

    - a **dataset-scoped** field (``library.chemistry``, ``library.prep_type``,
      ``experiment.organism``, ``experiment.accessions``) stays ONE Assertion. ``chemistry_hypothesis``
      reduces N identical claims to one regardless, so what the grep buys is the proof of unanimity,
      not a claim per record.
    - a **sample-scoped** field (the nine ``experiment.samples.*``) is **materialized once per
      member**, each with that member's own ``doc_sha256`` and offsets recomputed by
      :func:`~seqforge.harvest.verify.find_span` against that member's own text.

    Materializing rather than growing ``Assertion`` a subject list is the whole reason
    :func:`seqforge.resolve.records._basis_for` is untouched by this feature: a fanned claim cites a
    document whose ``subject`` is one record, so it maps home through ``subject_to_sample`` exactly as
    an unfanned one does and stays **asserted** rather than degrading to ``inferred``. No model
    change, no ``schema export`` movement, and no downstream component learns a new concept.

    Only the **exemplar's** claims fan. A second reading of byte-identical text is the same evidence,
    and fanning two readings of it would manufacture a disagreement out of model variance rather than
    out of bytes — which ``resolve_metadata`` would then report as a Conflict against nobody.
    """
    by_sha = {d.doc_sha256: d for d in plan.documents}
    out: list[Assertion] = []
    fanned: list[FannedClaim] = []

    for n, claim in enumerate(assertions):
        out.append(claim)
        doc = by_sha.get(claim.span.doc_sha256)
        group = plan.collapsed.get(claim.span.doc_sha256)
        start, end = claim.span.char_start, claim.span.char_end
        if doc is None or group is None or start is None or end is None:
            continue
        if not is_invariant_span(doc, start, end):
            continue  # the quote touches a place the members disagree: it speaks for this one only
        materialize = claim.field in _SAMPLE_SCOPED_FIELDS
        reached = list(plan.members.get(doc.doc_sha256, ()))
        ids = [claim.id]
        # Every member, reduced and withheld alike: what a member COST decides which document carries
        # its difference, and never whether the invariant holds of its bytes.
        for other in group.members:
            found = find_span(other.text, claim.span.quote)
            if found is None:
                # Unreachable while the invariant holds — and checked anyway, because "by
                # construction" is exactly the phrase every silent fan-out defect hides behind.
                continue
            reached.extend(plan.members.get(other.doc_sha256, ()))
            if not materialize:
                continue
            out.append(
                Assertion(
                    id=f"assert-{other.doc_sha256[:8]}-fan{n}",
                    field=claim.field,
                    value=claim.value,
                    span=SourceSpan(
                        doc_sha256=other.doc_sha256,
                        quote=claim.span.quote,
                        context=claim.span.context,
                        # Recomputed against THIS member's text, never copied: an earlier variant of a
                        # different length shifts every offset after it, so the exemplar's numbers
                        # would point at the wrong characters in a document that carries the same
                        # quote.
                        char_start=found[0],
                        char_end=found[1],
                        page=page_for_offset(other.pages, found[0]),
                    ),
                    span_verified=True,
                    entailment_ok=True,
                    llm_confidence=claim.llm_confidence,
                    extractor=claim.extractor,
                )
            )
            ids.append(out[-1].id)
        if len(reached) > len(plan.members.get(doc.doc_sha256, ())):
            fanned.append(
                FannedClaim(
                    assertion_ids=tuple(ids),
                    field=claim.field,
                    value=claim.value,
                    quote=claim.span.quote,
                    source_doc_sha256=doc.doc_sha256,
                    records=tuple(reached),
                    materialized=materialize,
                )
            )
    return FanReport(assertions=out, fanned=fanned)


@dataclass(frozen=True)
class RequestResidue:
    """One request's share of the residue: how much of what it carries is ambiguous inside it.

    Counted **per document, not per request**, and that is the whole difference between an instrument
    and a number that looks like one. Count each distinct span once per REQUEST and the totals fall as
    the batch widens — because there are fewer requests — which reads as the hazard shrinking when it
    is doing the opposite. Per document, ``n_spans`` is fixed by the plan and only ``n_ambiguous``
    moves, so the rate is monotone in width, which is the claim the measurement exists to test.
    """

    request: int
    n_documents: int
    #: Distinct quotable spans, summed over this request's documents.
    n_spans: int
    #: Of those, the ones that also occur in another document of the SAME request.
    n_ambiguous: int


@dataclass(frozen=True)
class QuoteResidue:
    """How often a quote in this plan could span-verify against the WRONG document of its request.

    The failure this measures is not the collapse's: a batch puts several documents in one prompt and
    the model routes each draft by echoing a ``doc_sha256``, so a quote occurring verbatim in two
    members of the same request verifies either way and nothing downstream can tell. Widening a batch
    can only make that residue larger, and a collapse changes which documents share a request — so the
    number is worth having before the ``--llm`` probe rather than after it.

    Deterministic: no model, no network, and a **lower bound**. A shorter quote is likelier to collide
    than a longer one, so counting only spans of ``min_tokens`` whole tokens under-counts; the point is
    the growth curve against ``width``, not the absolute rate.
    """

    #: The batch width measured. ``None`` = the plan's own batching.
    width: int | None
    min_tokens: int
    n_requests: int
    n_documents: int
    n_spans: int
    n_ambiguous: int
    per_request: tuple[RequestResidue, ...]

    @property
    def rate(self) -> float:
        return self.n_ambiguous / self.n_spans if self.n_spans else 0.0


def quote_residue(
    plan: ExtractionPlan, *, width: int | None = None, min_tokens: int = 4
) -> QuoteResidue:
    """Count the spans of this plan that occur in more than one document of the same request.

    ``width`` re-batches the plan's documents into fixed windows *of the same ask*, mirroring
    :func:`batch_documents` without depending on either of its caps — so "how does the residue grow
    with batch width" is one call per width and needs no constant edited. ``None`` measures the
    batching the plan would really send.
    """
    batches = _windows(plan.documents, width) if width else plan.batches
    rows: list[RequestResidue] = []
    for n, batch in enumerate(batches):
        per_document = [set(_token_windows(plan.documents[i].text, min_tokens)) for i in batch]
        carried: dict[str, int] = {}
        for spans in per_document:
            for span in spans:
                carried[span] = carried.get(span, 0) + 1
        rows.append(
            RequestResidue(
                request=n,
                n_documents=len(batch),
                n_spans=sum(len(s) for s in per_document),
                n_ambiguous=sum(sum(1 for s in spans if carried[s] > 1) for spans in per_document),
            )
        )
    return QuoteResidue(
        width=width,
        min_tokens=min_tokens,
        n_requests=len(rows),
        n_documents=sum(r.n_documents for r in rows),
        n_spans=sum(r.n_spans for r in rows),
        n_ambiguous=sum(r.n_ambiguous for r in rows),
        per_request=tuple(rows),
    )


def _token_windows(text: str, n: int) -> Iterator[str]:
    """Every run of ``n`` consecutive whole tokens, with the separators between them, as substrings."""
    spans = token_spans(text)
    for i in range(len(spans) - n + 1):
        yield text[spans[i][0] : spans[i + n - 1][1]]


def _windows(documents: Sequence[NormalizedDoc], width: int) -> tuple[tuple[int, ...], ...]:
    """Plan-ordered indices chunked into fixed-width groups **of one ask** — a measurement batching,
    never a send one, so it reads none of the send-time bounds. Sweeping the residue against width is
    the whole point of the instrument, and a sweep that stopped at whatever the shipped batcher would
    have chosen could not answer the question it exists for."""
    by_ask: dict[tuple[str, ...], list[int]] = {}
    for i, doc in enumerate(documents):
        by_ask.setdefault(fields_for(doc.scope, doc.role), []).append(i)
    out = [
        tuple(group[i : i + width])
        for group in by_ask.values()
        for i in range(0, len(group), width)
    ]
    return tuple(sorted(out, key=lambda b: b[0]))


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
    "CollapsedGroup",
    "ExtractionPlan",
    "FanReport",
    "FannedClaim",
    "QuoteResidue",
    "RequestResidue",
    "batch_documents",
    "batch_max_tokens",
    "batch_width",
    "extract_planned",
    "fan_claims",
    "plan_extraction",
    "quote_residue",
]
