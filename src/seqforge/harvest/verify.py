"""``harvest verify`` — the hallucination tripwire, owned by code and failing closed (R1/R5).

The LLM emits only ``{field, value, quote}``; it never emits offsets, because models cannot count
characters and a wrong offset would reject a truthful claim. Code searches the normalized text,
computes the offsets, and sets **both** verification flags. An Assertion only reaches ``manifest fill``
if all three hold:

- ``field`` is in the allowlist — the claim names a manifest path the model may set at all. This is
  the *only* check that is not about the document, and it is the one the other two cannot stand in
  for: see :mod:`seqforge.harvest.fields` for the real quote that entails a real value on a field
  nobody ever authorized.
- ``span_verified``  — the quote really occurs in the cited document. Catches **fabricated provenance**.
- ``entailment_ok``  — the quote actually *supports the value*. Catches the more common and more
  dangerous failure: a **real quote mis-attached to a wrong value** (a verbatim "single-cell RNA-seq"
  span pinned to "10x 3' v3.1"). Without this, span-verification alone is theatre — a model can quote
  the paper faithfully and still invent the conclusion.

All three are deterministic. Anything unverifiable is rejected, never waved through.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ..kb import load_all_specs
from ..models.assertion import Assertion, AssertionDraft, ExtractorProvenance, SourceSpan
from .fields import PERMITTED_FIELDS, permitted_for_role
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


def _kb_chemistry_aliases(value: str) -> list[str]:
    """A paper says "Chromium Single Cell 3' v3", not "10x-3p-gex-v3". The KB curates those aliases."""
    forms: list[str] = []
    for tech_id, spec in load_all_specs().items():
        candidates = {tech_id, spec.identity.id, spec.identity.name, *spec.identity.aliases}
        if any(_squash(c).lower() == _squash(value).lower() for c in candidates):
            forms.extend(candidates)
    return forms


#: field -> extra acceptable surface forms. EXACT-match dispatch, replacing a substring test
#: (`if "chemistry" in field or "assay" in field`) that would have misfired the moment a field was
#: named e.g. `processing.assay_override`.
#:
#: **`processing.quantification` is deliberately absent, and that absence is the design.** Its values
#: are STARsolo's own spellings, so a quote naming the feature already matches the bare value —
#: `entails` lowercases and substring-matches, so "pass --soloFeatures GeneFull" needs no alias. An
#: alias table here could therefore only ever LOOSEN the check, and loosening is exactly the hazard:
#: teach it `"nuclei" -> GeneFull` and the sentence "we prepared single nuclei" entails a processing
#: decision, at which point R5 is theatre — the model would be INFERRING the decision from a biological
#: fact, and that inference is code's to own. The instruction document's documented contract is **name
#: the STARsolo feature**; "count introns too" is correctly rejected as not-entailed.
#:
#: That rigor is affordable only because of R15: the case that most tempts you to add the alias — a
#: nuclear prep — is the case the all-five default already covers. If the default ever narrows,
#: revisit this comment at the same time.
_ALIAS_SOURCES: dict[str, Callable[[str], list[str]]] = {
    "library.chemistry": _kb_chemistry_aliases,
    "library.assay": _kb_chemistry_aliases,
}


def surface_forms(field: str, value: str) -> list[str]:
    """Acceptable surface forms for a value — the value itself plus any curated aliases for its field."""
    extra = _ALIAS_SOURCES.get(field)
    return list(dict.fromkeys([value, *(extra(value) if extra else [])]))


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
        # FIRST, and before anything about the document's CONTENT: may the model set this field at
        # all? A quote can be real and entailing on a field that was never on offer, so no amount of
        # document-checking below can substitute for this one (see .fields).
        if draft.field not in PERMITTED_FIELDS:
            rejected.append(
                _reject(draft, "field_not_permitted", f"{draft.field!r} is not an assertable field")
            )
            continue
        doc = by_sha.get(draft.span.doc_sha256)
        if doc is not None and not permitted_for_role(draft.field, doc.role):
            # ...and may it set this field from THIS document? A downloaded methods PDF may never
            # steer the pipeline. Role is code-owned (the flag it arrived under), so this is a
            # deterministic refusal, not a judgement about the sentence.
            rejected.append(
                _reject(
                    draft,
                    "field_not_permitted_for_doc_role",
                    f"{draft.field!r} may only be set by an --instruction document, and "
                    f"{doc.source_basename!r} is a {doc.role}",
                )
            )
            continue
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
