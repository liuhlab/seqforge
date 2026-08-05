"""``Assertion`` — the LLM's structured output (job a), span-verified by deterministic code.

The LLM emits an :class:`AssertionDraft` (``field``, ``value``, a ``quote``), never character
offsets — LLMs cannot count them. Deterministic code searches the normalized document for the quote,
computes offsets, and sets the two verification flags, so a hallucinated or mis-attributed claim
fails closed.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .base import Confidence


class SourceSpan(BaseModel):
    """Exact, greppable provenance for one claim. Offsets are COMPUTED by code, not the LLM.

    ``page`` is the same kind of field as ``char_start``/``char_end``: code-owned, computed by
    ``verify`` from the offset against the document's page index, and the LLM's value (if any) is
    discarded. It is the 1-indexed physical page of a PDF, and ``None`` for an unpaged source (a
    ``.txt``/``.xlsx`` or an archive record) — so a citation can say "p.4" only when that is a real,
    checkable location.
    """

    doc_sha256: str
    quote: str
    context: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    page: int | None = None


class AssertionDraft(BaseModel):
    """The ONLY LLM structured-output surface for harvest (job a).

    Kept trivially simple — no unions, no offsets, ``value`` is a plain string — so it stays inside
    the provider strict-schema subset.
    """

    field: str
    value: str
    span: SourceSpan
    llm_confidence: Confidence


class ExtractorProvenance(BaseModel):
    """Which model + prompt produced an assertion (makes a harvest reproducible and blamable)."""

    model_id: str
    prompt_version: str


class Assertion(BaseModel):
    """A stored, code-composed assertion. Both verification flags are code-owned (fail-closed).

    ``span_verified`` catches fabricated provenance; ``entailment_ok`` catches a real quote
    mis-attached to a wrong value. Both must hold before an assertion flows into ``manifest fill``.
    """

    id: str
    field: str
    value: str
    span: SourceSpan
    span_verified: bool = False
    entailment_ok: bool = False
    llm_confidence: Confidence
    extractor: ExtractorProvenance


class PlannedDocument(BaseModel):
    """One document an extraction will pay for, described before it is sent.

    **Two member lists, because a collapse produces two facts and one list cannot hold both.**
    ``members`` is every archive record this document is the ONLY reading of — its own, the runs of
    one sample folded into it, and a near-identical member *withheld* onto it because its whole
    difference was the accession we ourselves wrote. ``reduced_members`` is every near-identical
    record that shares this document's prose and was sent **its own difference**, as a document of
    its own further down the same list (ADR-0031). A reader handed only the union would take "one
    document, 1440 members" for 1439 records that went unread, which is the exact opposite of the
    guarantee the collapse is named for; every record a plan reads appears in exactly one document's
    ``members``, and summing that column is how a reader checks it rather than trusting it.

    Both are the DOCUMENT side of the collapse's legibility. The claim side is ``harvest extract``'s
    ``fanned`` rows, which say how many records each value was fanned to. **This is the one place
    that argument is made**: at either count every claim is verified in the record it names — a
    fanned claim's quote greps into the record it lands on by construction — so **no N here moves the
    epistemics**. What N moves is what a human is being asked to audit, because "one assertion, 1440
    members" is a different thing to check than 1440 independent readings (#233 decision 6). Every
    other count in this mechanism glosses this and points back.
    """

    doc_sha256: str
    source: str
    role: str
    scope: str
    #: The record this document speaks for — a sample's accession on a collapsed run document,
    #: because that is the sample its claims are declarations about. ``None`` for a dataset document.
    subject: str | None = None
    n_chars: int
    fields: list[str]
    members: list[str] = Field(default_factory=list)
    #: Near-identical records that were sent their distinctive bytes instead — each of them is its
    #: own ``PlannedDocument`` in this same report, so they were read, not folded away. Empty unless
    #: this document is a collapse's exemplar.
    reduced_members: list[str] = Field(default_factory=list)


class ExtractionPlanReport(BaseModel):
    """What ``harvest extract --dry-run`` answers: the whole ask, costed, with nothing spent.

    A dataset's cost used to be a property nobody computed until it had been paid. ``n_requests`` is
    the exchange count (before retries) and ``estimated_input_tokens`` charges the stable system
    prefix once **per request** — which is what a fan-out over one-line archive records used to make
    expensive, and what batching same-ask documents buys back. Output tokens are not estimated: the
    model decides how many claims a document supports, and the token Ceiling bounds that half.

    ``n_documents`` and ``n_requests`` are reported apart because they answer different questions —
    how much material was read, and how many times a model is reached. They were the same number
    until documents began sharing a request, and a reader who has only the first cannot tell a plan
    that batched well from one that could not batch at all.
    """

    n_documents: int
    #: Requests this plan will issue, before retries — the floor on ``llm_calls``. Never more than
    #: ``n_documents``, and fewer wherever two documents receive the same ask.
    n_requests: int = 0
    #: Archive records with prose that this plan reads. A level asked nothing (``project``) and a
    #: record with no free text are not read, and are not counted here.
    n_records_read: int = 0
    #: Records read but not costing a document of their own — the runs folded into their sample's
    #: document, and a near-identical record whose only difference is the accession we ourselves wrote.
    n_records_collapsed: int = 0
    #: Records sent as their DISTINCTIVE BYTES only: the invariant they share was read once, in the
    #: exemplar. A separate number from ``n_records_collapsed`` because they are separate facts — a
    #: record that cost nothing, against a record that cost what it is worth (ADR-0031).
    n_records_reduced: int = 0
    n_chars: int = 0
    system_prompt_chars: int = 0
    estimated_input_tokens: int = 0
    documents: list[PlannedDocument] = Field(default_factory=list)
