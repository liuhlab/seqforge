"""``harvest verify`` — the hallucination tripwire, owned by code and failing closed (R1/R5).

The LLM emits only ``{field, value, quote}``; it never emits offsets, because models cannot count
characters and a wrong offset would reject a truthful claim. Code searches the normalized text,
computes the offsets, and sets **both** verification flags. An Assertion only reaches ``manifest fill``
if both hold:

- ``span_verified``  — the quote really occurs in the cited document. Catches **fabricated provenance**.
- ``entailment_ok``  — the quote actually *supports the value*. Catches the more common and more
  dangerous failure: a **real quote mis-attached to a wrong value** (a verbatim "single-cell RNA-seq"
  span pinned to "10x 3' v3.1"). Without this, span-verification alone is theatre — a model can quote
  the paper faithfully and still invent the conclusion.

Both checks are deterministic. Anything unverifiable is rejected, never waved through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..kb import load_all_specs
from ..models.assertion import Assertion, AssertionDraft, ExtractorProvenance, SourceSpan
from .normalize import NormalizedDoc

_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9']+")
#: tokens too generic to carry entailment weight on their own
_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "for", "with", "and", "or", "was", "were", "used", "using", "kit"}
)


@dataclass(frozen=True)
class VerifyReport:
    """Outcome of verifying a batch of drafts: what survived, and precisely why the rest did not."""

    assertions: list[Assertion]
    rejected: list[dict[str, object]]

    @property
    def n_accepted(self) -> int:
        return len(self.assertions)


def _squash(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower().replace("-", " "))


def find_span(text: str, quote: str) -> tuple[int, int] | None:
    """Locate ``quote`` in ``text``, tolerating whitespace differences. Returns ``(start, end)``.

    Exact first; then a whitespace-flexible regex, because a quote copied across a line wrap differs
    from the canonical text only in runs of space — rejecting that would punish an honest quote.
    """
    if not quote.strip():
        return None
    idx = text.find(quote)
    if idx >= 0:
        return idx, idx + len(quote)
    squashed = _squash(quote)
    idx = text.find(squashed)
    if idx >= 0:
        return idx, idx + len(squashed)
    pattern = r"\s+".join(re.escape(part) for part in squashed.split(" ") if part)
    if not pattern:
        return None
    match = re.search(pattern, text)
    return (match.start(), match.end()) if match else None


def surface_forms(field: str, value: str) -> list[str]:
    """Acceptable surface forms for a value — the value itself plus, for chemistry, its KB aliases.

    A paper says "Chromium Single Cell 3' v3", not "10x-3p-gex-v3". The KB already curates those
    aliases, so entailment consults the KB rather than inventing a synonym table.
    """
    forms = [value]
    if "chemistry" in field or "assay" in field:
        for tech_id, spec in load_all_specs().items():
            candidates = {tech_id, spec.identity.id, spec.identity.name, *spec.identity.aliases}
            if any(_squash(c).lower() == _squash(value).lower() for c in candidates):
                forms.extend(candidates)
    return list(dict.fromkeys(forms))


def entails(quote: str, field: str, value: str) -> bool:
    """Does ``quote`` support ``value``? True iff some surface form is carried by the quote.

    A form matches when it is a substring, or when all of its significant tokens appear in the quote
    (order-independent — "Chromium Single Cell 3' v3" carries the alias "Chromium 3' v3"). Purely
    generic tokens cannot entail on their own, so a quote saying only "single-cell RNA-seq" can never
    entail a specific chemistry version.

    **Know what this check cannot do.** Its power comes entirely from ``value`` being drawn from a
    controlled vocabulary: for ``library.chemistry`` the value is a KB id, so the quote must contain a
    real alias, and a quote about "droplet-based single-cell" cannot smuggle in a v3 chemistry. For a
    free-text field the model supplies a value copied *out of* the quote, so ``form in quote`` is
    trivially true and this returns True for anything. **Entailment is vacuous when value ⊆ quote.**

    So R5 is a tripwire for fabricated and mis-attributed claims, NOT for field-assignment errors. A
    real quote filed under the wrong field passes here by construction — `eval run --llm` caught
    exactly that (worm husbandry filed as an experimental `condition`). The defense for free-text
    fields is the prompt's operational definition of the field plus the evals corpus that measures it,
    not this function. Tightening the matcher would not help; there is nothing here left to check.
    """
    q = _squash(quote).lower()
    q_tokens = set(_tokens(q))
    for form in surface_forms(field, value):
        f = _squash(form).lower()
        if f and f in q:
            return True
        f_tokens = [t for t in _tokens(f) if t not in _STOPWORDS]
        if f_tokens and set(f_tokens) <= q_tokens:
            return True
    return False


def verify_drafts(
    drafts: list[AssertionDraft],
    docs: list[NormalizedDoc],
    *,
    extractor: ExtractorProvenance,
) -> VerifyReport:
    """Compose code-owned :class:`Assertion`s from LLM drafts. Only fully-verified claims survive."""
    by_sha = {d.doc_sha256: d for d in docs}
    assertions: list[Assertion] = []
    rejected: list[dict[str, object]] = []

    for i, draft in enumerate(drafts):
        doc = by_sha.get(draft.span.doc_sha256)
        if doc is None:
            rejected.append(
                _reject(draft, "unknown_doc", f"no normalized doc {draft.span.doc_sha256}")
            )
            continue
        found = find_span(doc.text, draft.span.quote)
        if found is None:
            # fabricated provenance: the quote is not in the document it cites
            rejected.append(
                _reject(draft, "span_not_found", "quote does not occur in the document")
            )
            continue
        if not entails(draft.span.quote, draft.field, draft.value):
            # real quote, wrong value — the failure span-verification alone would miss
            rejected.append(
                _reject(draft, "not_entailed", f"quote does not support value {draft.value!r}")
            )
            continue
        start, end = found
        assertions.append(
            Assertion(
                id=f"assert-{draft.span.doc_sha256[:8]}-{i}",
                field=draft.field,
                value=draft.value,
                span=SourceSpan(
                    doc_sha256=draft.span.doc_sha256,
                    quote=draft.span.quote,
                    context=draft.span.context,
                    char_start=start,  # computed by code, never by the model
                    char_end=end,
                ),
                span_verified=True,
                entailment_ok=True,
                llm_confidence=draft.llm_confidence,
                extractor=extractor,
            )
        )
    return VerifyReport(assertions=assertions, rejected=rejected)


def _reject(draft: AssertionDraft, reason: str, detail: str) -> dict[str, object]:
    return {
        "field": draft.field,
        "value": draft.value,
        "quote": draft.span.quote[:120],
        "reason": reason,
        "detail": detail,
    }
