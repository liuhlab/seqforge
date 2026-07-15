"""``harvest extract`` — **the one LLM touchpoint in the whole compiler** (R1/R2).

Everything else in seqforge is a verifier. This module's entire job is to turn prose into
``AssertionDraft{field, value, quote}`` candidates. It decides nothing:

- **No offsets.** The model cannot count characters, so it never emits them — ``verify`` greps the
  quote and computes them (a model-supplied offset would reject truthful claims).
- **No provenance identity.** ``span.doc_sha256`` is **overwritten by code** after parsing: we know
  which document we sent, so a fabricated or mistyped sha is not a failure mode we need to have.
- **No verdicts.** The model never asserts that its own quote is real or supportive; ``verify`` owns
  both flags and fails closed.

The schema is derived from the canonical Pydantic model (``AssertionDraft``) by the SDK, not
hand-maintained (R1 / design §1.8) — so the wire contract cannot drift from ``models/``. The SDK
strips the constraints the strict-schema subset rejects (``Confidence``'s 0..1 bounds) and re-validates
them client-side, which is exactly the "constraints live in the canonical schema; Pydantic enforces
them at ingest" rule.

Prompt caching: the KB context is the stable prefix (it changes only when the KB does) and the
document is volatile, so the system blocks carry the cache breakpoint and the document goes in the
user turn. Note Opus 4.8 will not cache a prefix under ~4096 tokens — check
``usage.cache_read_input_tokens`` rather than assuming.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from ..kb.schema import Spec
from ..models.assertion import AssertionDraft, ExtractorProvenance, SourceSpan
from .normalize import NormalizedDoc

#: CalVer-ish; bump on ANY prompt change — it is folded into ExtractorProvenance so a harvest is
#: reproducible and blamable, and evals treat a prompt edit as a code change (brief §9).
EXTRACT_PROMPT_VERSION = "2026.7.0"

#: Opus 4.8. Never downgrade for cost without the maintainer asking — a cheaper miss here is a
#: silently thinner corpus, which is the failure mode this project exists to prevent.
DEFAULT_MODEL = "claude-opus-4-8"

#: Manifest paths worth asking for. `library.*` is byte-decidable and only ever a HYPOTHESIS here
#: (resolve owns the decision); `experiment.*` is the part bytes genuinely cannot see.
DEFAULT_FIELDS = (
    "library.chemistry",
    "experiment.organism",
    "experiment.accessions",
    "experiment.samples.tissue",
    "experiment.samples.condition",
)

_SYSTEM_INSTRUCTIONS = """\
You extract factual claims from a scientific methods document into structured assertions.

You are one stage of a deterministic compiler. Downstream code independently re-greps every quote you
produce and checks that the quote supports the value. Claims that fail either check are DISCARDED, so
inventing or stretching a claim gains nothing — it only wastes the extraction.

Rules:
1. Extract ONLY what the document explicitly states. Never use background knowledge, never infer, and
   never complete a pattern. If the document does not state a field, omit it.
2. `quote` must be a VERBATIM, contiguous substring copied from the document text, exactly as it
   appears. Do not paraphrase, normalize, join across a gap, or fix typos.
3. The quote must, ON ITS OWN, support the value. A reader seeing only that quote must be able to
   conclude the value from it. A quote that merely sits near the fact is not enough: for example,
   "we performed single-cell RNA-seq" does NOT support a specific chemistry version.
4. Keep the quote tight — the shortest span that still supports the value.
5. Return an empty list if the document supports nothing. That is a CORRECT and common answer.
6. Never emit character offsets. Code computes them.
7. `llm_confidence` is how sure you are that the document states the claim — not how plausible the
   claim is in general.

Values:
- `library.chemistry`: use the knowledge-base `id` when the document names that technology by any of
  its aliases. If the document names a technology not in the knowledge base, use the document's own
  wording.
- `experiment.organism`: the scientific name as written (e.g. "Caenorhabditis elegans").
- everything else: the document's own wording.
"""


class ExtractUnavailable(RuntimeError):
    """The LLM surface cannot be reached (SDK missing, no credential, or the API failed)."""


class ExtractionResult(BaseModel):
    """The model's structured-output surface: a batch of drafts and nothing else.

    A thin container over the canonical :class:`AssertionDraft` (structured outputs need an object at
    the top level) — deliberately NOT a second hand-maintained schema.
    """

    drafts: list[AssertionDraft]


@dataclass(frozen=True)
class ExtractionOutcome:
    """What extract returns: the drafts, who made them, and what the call cost."""

    drafts: list[AssertionDraft]
    extractor: ExtractorProvenance
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def cache_hit(self) -> bool:
        """True iff the KB prefix was served from cache (0 across repeated calls => an invalidator)."""
        return self.usage.get("cache_read_input_tokens", 0) > 0


def build_kb_context(specs: dict[str, Spec]) -> str:
    """The stable prefix: what each KB technology is called in the wild.

    Deterministic and frozen — sorted, no timestamps, no per-request ids — because prompt caching is a
    prefix match and any byte change invalidates it. This is the alias knowledge that lets the model
    map a paper's "Chromium Single Cell 3' v3" onto the id `10x-3p-gex-v3`; `verify` then checks the
    same aliases from the same KB, so extraction and verification cannot disagree about vocabulary.
    """
    lines = ["Knowledge-base technologies (use these ids for library.chemistry):", ""]
    for tech_id in sorted(specs):
        spec = specs[tech_id]
        aliases = ", ".join(spec.identity.aliases) if spec.identity.aliases else "(none)"
        lines += [
            f"id: {spec.identity.id}",
            f"  name: {spec.identity.name}",
            f"  aliases: {aliases}",
        ]
    return "\n".join(lines)


def _user_content(doc: NormalizedDoc, fields: tuple[str, ...]) -> str:
    return (
        f"Document sha256: {doc.doc_sha256}\n"
        f"Echo that exact string as span.doc_sha256 on every assertion.\n\n"
        f"Fields to look for (omit any the document does not state):\n"
        + "\n".join(f"- {f}" for f in fields)
        + "\n\n<document>\n"
        + doc.text
        + "\n</document>"
    )


def _default_client() -> Any:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise ExtractUnavailable(
            "the `anthropic` SDK is not installed; harvest extract is the only stage that needs it"
        ) from exc
    return anthropic.Anthropic()


def extract_drafts(
    doc: NormalizedDoc,
    specs: dict[str, Spec],
    *,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    fields: tuple[str, ...] = DEFAULT_FIELDS,
    max_tokens: int = 8000,
) -> ExtractionOutcome:
    """Ask the model for span-carrying claims about ``doc``. Proposes only — ``verify`` decides."""
    client = client if client is not None else _default_client()
    system = [
        {"type": "text", "text": _SYSTEM_INSTRUCTIONS},
        # cache breakpoint on the LAST system block: render order is tools -> system -> messages, so
        # this caches the instructions + KB context together while the document stays uncached.
        {
            "type": "text",
            "text": build_kb_context(specs),
            "cache_control": {"type": "ephemeral"},
        },
    ]
    try:
        response = client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": _user_content(doc, fields)}],
            output_format=ExtractionResult,
            thinking={"type": "adaptive"},
        )
    except ExtractUnavailable:
        raise
    except Exception as exc:  # SDK/network/API failure — a refusal to guess, not a crash
        raise ExtractUnavailable(f"extraction call failed: {exc}") from exc

    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        raise ExtractUnavailable("model returned no parseable structured output")

    extractor = ExtractorProvenance(model_id=model, prompt_version=EXTRACT_PROMPT_VERSION)
    return ExtractionOutcome(
        drafts=[_anchor(d, doc) for d in parsed.drafts],
        extractor=extractor,
        usage=_usage(response),
    )


def _anchor(draft: AssertionDraft, doc: NormalizedDoc) -> AssertionDraft:
    """Force every draft onto the document we actually sent.

    We know which document this was; the model's echo of the sha is therefore worthless as evidence
    and dangerous as a failure mode (a mistyped sha would be rejected downstream as `unknown_doc`,
    which looks like a hallucination but is just a typo). Code owns provenance identity — the same
    reason code owns the offsets.
    """
    return draft.model_copy(
        update={
            "span": SourceSpan(
                doc_sha256=doc.doc_sha256,
                quote=draft.span.quote,
                context=draft.span.context,
                # offsets stay None here on purpose: `verify` computes them from the real text
            )
        }
    )


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    return {k: int(getattr(usage, k, 0) or 0) for k in keys}
