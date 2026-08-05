"""``harvest`` — prose/metadata -> span-verified :class:`Assertion`s (the ONE LLM touchpoint).

Three verbs, and only the middle one touches a model:

- ``normalize`` (deterministic) — source docs -> the canonical text that spans are computed against.
- ``extract``   (**LLM**)       — normalized text (+ KB prose) -> ``AssertionDraft[]``. The model emits
  ``{field, value, quote}`` and nothing else: no offsets (it cannot count characters) and no verdicts.
- ``verify``    (deterministic) — checks the field is one we allow at all, greps each quote back into
  the canonical text, computes the offsets, and checks the quote actually *entails* the value. Every
  flag is code-owned, so a hallucinated or mis-attributed claim fails closed.

Agents propose; code decides. Nothing here trusts the model's own account of its work — including its
account of *which field it is answering*: the vocabulary lives in :mod:`seqforge.harvest.fields` and is
enforced there, not in the prompt. A prompt asks; only code refuses.
"""

from __future__ import annotations

#: CalVer YYYY.M.PATCH; bumped when harvest semantics change. Folded into artifact cache keys.
#: 2026.8.0: near-identical archive records collapse at plan time, so the send list itself moved — a
#: cached harvest computed before the collapse asked documents this one folds away, and its claims
#: were never fanned to the records their quotes grep into.
HARVEST_VERSION = "2026.8.0"

from .extract import (  # noqa: E402
    EXTRACT_PROMPT_VERSION,
    ExtractionOutcome,
    ExtractionResult,
    ExtractUnavailable,
    build_kb_context,
    build_system_prompt,
    document_sha256_in,
    extract_drafts,
    llm_schema,
)
from .fields import (  # noqa: E402
    ASKED_SAMPLE_ATTRIBUTES,
    DEFAULT_FIELDS,
    PERMITTED_FIELDS,
    DocScope,
    is_permitted,
)
from .meter import (  # noqa: E402
    RAW_KEYS,
    CeilingExceeded,
    Exchange,
    TokenMeter,
    Transcript,
    raw_tokens,
)
from .normalize import (  # noqa: E402
    DEFAULT_PDF_BACKEND,
    NORMALIZER_VERSION,
    DeclaredSpan,
    NormalizedDoc,
    PageSpan,
    PdfBackend,
    UnreadableDocument,
    VariantSpan,
    clean_invalid_unicode,
    has_prose,
    normalize_document,
    normalize_record,
    normalize_text,
    page_for_offset,
    read_document,
    render_record,
)
from .plan import (  # noqa: E402
    CHARS_PER_TOKEN,
    MAX_IN_FLIGHT,
    CollapsedGroup,
    ExtractionPlan,
    FannedClaim,
    FanReport,
    QuoteResidue,
    RequestResidue,
    extract_planned,
    fan_claims,
    plan_extraction,
    quote_residue,
)
from .providers import (  # noqa: E402
    ANTHROPIC_DEFAULT_MODEL,
    DEEPSEEK_DEFAULT_MODEL,
    DEEPSEEK_FLASH_MODEL,
    DEEPSEEK_MODELS,
    DEEPSEEK_PRO_MODEL,
    AnthropicProvider,
    LLMProvider,
    LLMResponse,
    OpenAICompatibleProvider,
    ProviderUnavailable,
    deepseek_provider,
    resolve_provider,
)
from .transcript import (  # noqa: E402
    TRANSCRIPT_FILENAME,
    read_transcript,
    write_transcript,
)
from .verify import (  # noqa: E402
    VerifyReport,
    entails,
    find_span,
    surface_forms,
    verify_drafts,
)

__all__ = [
    "HARVEST_VERSION",
    "NORMALIZER_VERSION",
    "DEFAULT_PDF_BACKEND",
    "PdfBackend",
    "NormalizedDoc",
    "PageSpan",
    # A mark's TYPE is here because it types a field of `NormalizedDoc`, which every consumer of a
    # document already holds. The functions that COMPUTE one -- `declared_spans`, `variant_spans`,
    # `variant_text`, `is_invariant_span` and the tokenizer under them -- are the planner's and
    # nobody else's, so they stay `seqforge.harvest.normalize`'s and are imported from there. It is
    # the rule the CLI keeps one layer up: a surface is what a caller acts through, never an
    # inventory of what the mechanism happens to be made of.
    "DeclaredSpan",
    "VariantSpan",
    "UnreadableDocument",
    "normalize_document",
    "normalize_record",
    "render_record",
    "has_prose",
    "normalize_text",
    "clean_invalid_unicode",
    "page_for_offset",
    "read_document",
    "VerifyReport",
    "verify_drafts",
    "find_span",
    "entails",
    "surface_forms",
    # extract (the one LLM touchpoint)
    "EXTRACT_PROMPT_VERSION",
    "DEFAULT_FIELDS",
    "PERMITTED_FIELDS",
    "is_permitted",
    "ASKED_SAMPLE_ATTRIBUTES",
    "DocScope",
    "extract_drafts",
    "document_sha256_in",
    "build_kb_context",
    "build_system_prompt",
    "llm_schema",
    "ExtractionResult",
    "ExtractionOutcome",
    "ExtractUnavailable",
    # the plan: records -> documents, costed before a token is spent
    "ExtractionPlan",
    "plan_extraction",
    "extract_planned",
    # the collapse: near-identical records folded at PLAN time, and the claims that then fan
    "fan_claims",
    "FanReport",
    "FannedClaim",
    "CollapsedGroup",
    # `quote_residue` reaches no CLI verb, ON PURPOSE, and is exported anyway: it is a measurement
    # instrument rather than a stage. It is what answered #283's recall probe -- 71 % of every
    # document's four-token spans occurred verbatim in another document of the same request before
    # the reduction, 0 % at every width after it (ADR-0031) -- and that is a property of one deposit,
    # not a theorem, so it has to stay runnable against the next one. A verb would put a maintainer's
    # ruler on the machine interface, every entry of which is a step the compiler itself takes.
    "quote_residue",
    "QuoteResidue",
    "RequestResidue",
    "CHARS_PER_TOKEN",
    "MAX_IN_FLIGHT",
    # providers (the LLM is swappable; nothing downstream trusts it)
    "LLMProvider",
    "LLMResponse",
    "AnthropicProvider",
    "OpenAICompatibleProvider",
    "deepseek_provider",
    "resolve_provider",
    "ProviderUnavailable",
    "ANTHROPIC_DEFAULT_MODEL",
    "DEEPSEEK_DEFAULT_MODEL",
    "DEEPSEEK_FLASH_MODEL",
    "DEEPSEEK_PRO_MODEL",
    "DEEPSEEK_MODELS",
    # the meter (counts every exchange, refuses past the Ceiling, holds the transcript)
    "TokenMeter",
    "CeilingExceeded",
    "Exchange",
    "Transcript",
    "raw_tokens",
    "RAW_KEYS",
    # the transcript's address on disk (the meter holds it; this writes and reads it)
    "TRANSCRIPT_FILENAME",
    "write_transcript",
    "read_transcript",
]
