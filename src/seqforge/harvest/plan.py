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
from .extract import ExtractionOutcome, ExtractUnavailable, extract_drafts
from .fields import DocScope, fields_for
from .normalize import (
    NormalizedDoc,
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

#: Characters per token, near enough to plan with. A plan is a warning about an order of magnitude —
#: "this dataset is 900 calls" — and a real tokenizer would buy a second decimal place nobody acts on
#: while adding a dependency and a model-specific answer to a question asked before a model is chosen.
CHARS_PER_TOKEN = 4


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
    def n_chars(self) -> int:
        """Document text this plan will send, in characters. The volatile half of the prompt."""
        return sum(len(d.text) for d in self.documents)

    @property
    def estimated_input_tokens(self) -> int:
        """A plan's whole point: the system prefix is charged **per document**.

        Output is not estimated and is not estimable — the model decides how many claims a document
        supports. The token Ceiling is what bounds that half; this bounds the half we choose.
        """
        return (self.n_documents * self.system_prompt_chars + self.n_chars) // CHARS_PER_TOKEN

    def asked(self, doc: NormalizedDoc) -> tuple[str, ...]:
        """The fields this document will be asked for. Extraction derives the same set."""
        return fields_for(doc.scope, doc.role)

    def report(self) -> ExtractionPlanReport:
        """The wire form: what a ``--dry-run`` prints, and the first-class result type it needs."""
        return ExtractionPlanReport(
            n_documents=self.n_documents,
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


def extract_planned(
    plan: ExtractionPlan,
    specs: dict[str, Spec],
    *,
    provider: LLMProvider,
    model: str | None = None,
    partial: bool = False,
) -> list[ExtractionOutcome]:
    """One extraction per planned document, concurrently, returned **in plan order**.

    A LIST, positionally aligned with ``plan.documents``, and that is the whole reassembly contract:
    a dict keyed by document identity loses one of two identical documents, and a completion-ordered
    list would make a run's graded assertions depend on which socket returned first.

    Each document is an independent, network-bound call, so they go out on a THREAD pool — the wait
    is on a socket, and processes would only add IPC. :data:`_SLOTS` is what the provider actually
    sees, because a caller may already be inside a pool of its own.

    ``partial`` decides what one document's failure costs the other twelve, and the two callers want
    opposite answers. **Off (the default) it raises**, so the compiler fails closed: an extraction
    missing a document produces a manifest that is silently short a fact, and there is no way to tell
    that manifest from a complete one afterwards. **On, an unanswered document comes back as an
    outcome carrying its** ``failure`` — because a harness measuring the stage learns nothing from
    twelve documents it declined to send, and a whole dataset raising on its one flaky supplementary
    table is how a graded tier reported nothing at all about five of its seven prose cases.

    A **Ceiling** is not caught either way. It is a refusal rather than an unavailability — the
    provider answered everything it was given — and the caller owes it a ``Blocker``, so it must
    still reach one.
    """

    def _one(doc: NormalizedDoc) -> ExtractionOutcome:
        with _SLOTS:
            if not partial:
                return extract_drafts(doc, specs, provider=provider, model=model)
            try:
                return extract_drafts(doc, specs, provider=provider, model=model)
            except ExtractUnavailable as exc:
                return ExtractionOutcome.unanswered(
                    provider=provider.name,
                    model=model or provider.default_model(),
                    detail=str(exc),
                )

    docs = plan.documents
    if len(docs) <= 1:
        return [_one(d) for d in docs]
    with ThreadPoolExecutor(max_workers=min(MAX_IN_FLIGHT, len(docs))) as pool:
        # `map` yields in submission order, so the result list matches `plan.documents` however the
        # calls interleave.
        return list(pool.map(_one, docs))


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
    "CHARS_PER_TOKEN",
    "MAX_IN_FLIGHT",
    "ExtractionPlan",
    "extract_planned",
    "plan_extraction",
]
