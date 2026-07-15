"""``harvest`` — prose/metadata -> span-verified :class:`Assertion`s (the ONE LLM touchpoint).

Three verbs, and only the middle one touches a model:

- ``normalize`` (deterministic) — source docs -> the canonical text that spans are computed against.
- ``extract``   (**LLM**)       — normalized text (+ KB prose) -> ``AssertionDraft[]``. The model emits
  ``{field, value, quote}`` and nothing else: no offsets (it cannot count characters) and no verdicts.
- ``verify``    (deterministic) — greps each quote back into the canonical text, computes the offsets,
  and checks the quote actually *entails* the value. Both flags are code-owned, so a hallucinated or
  mis-attributed claim fails closed (R1/R2/R5).

Agents propose; code decides. Nothing here trusts the model's own account of its work.
"""

from __future__ import annotations

#: CalVer YYYY.M.PATCH; bumped when harvest semantics change. Folded into artifact cache keys (R7).
HARVEST_VERSION = "2026.7.0"

from .extract import (  # noqa: E402
    DEFAULT_FIELDS,
    EXTRACT_PROMPT_VERSION,
    ExtractionOutcome,
    ExtractionResult,
    ExtractUnavailable,
    build_kb_context,
    build_system_prompt,
    extract_drafts,
    llm_schema,
)
from .normalize import (  # noqa: E402
    NORMALIZER_VERSION,
    NormalizedDoc,
    normalize_document,
    normalize_text,
    read_document,
)
from .providers import (  # noqa: E402
    ANTHROPIC_DEFAULT_MODEL,
    DEEPSEEK_DEFAULT_MODEL,
    AnthropicProvider,
    LLMProvider,
    LLMResponse,
    OpenAICompatibleProvider,
    ProviderUnavailable,
    deepseek_provider,
    resolve_provider,
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
    "NormalizedDoc",
    "normalize_document",
    "normalize_text",
    "read_document",
    "VerifyReport",
    "verify_drafts",
    "find_span",
    "entails",
    "surface_forms",
    # extract (the one LLM touchpoint)
    "EXTRACT_PROMPT_VERSION",
    "DEFAULT_FIELDS",
    "extract_drafts",
    "build_kb_context",
    "build_system_prompt",
    "llm_schema",
    "ExtractionResult",
    "ExtractionOutcome",
    "ExtractUnavailable",
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
]
