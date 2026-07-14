"""``Assertion`` — the LLM's structured output (job a), span-verified by deterministic code (R1/R5).

The LLM emits an :class:`AssertionDraft` (``field``, ``value``, a ``quote``), never character
offsets — LLMs cannot count them. Deterministic code searches the normalized document for the quote,
computes offsets, and sets the two verification flags, so a hallucinated or mis-attributed claim
fails closed.
"""

from __future__ import annotations

from pydantic import BaseModel

from .base import Confidence


class SourceSpan(BaseModel):
    """Exact, greppable provenance for one claim (R5). Offsets are COMPUTED by code, not the LLM."""

    doc_sha256: str
    quote: str
    context: str | None = None
    char_start: int | None = None
    char_end: int | None = None


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
