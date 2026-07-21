"""Provider layer — the LLM is a swappable component, not a foundation.

seqforge is a compiler whose only LLM touchpoint proposes claims that code then re-verifies from
first principles. That makes the provider genuinely pluggable: nothing downstream trusts the model,
so the choice is about cost and extraction quality, never about correctness guarantees.

Two providers ship:

- ``anthropic``          — strict ``json_schema`` structured output; the returned shape is guaranteed.
- ``openai-compatible``  — any OpenAI-shaped endpoint via ``base_url``. **DeepSeek** is a preset; so
  are vLLM, Ollama, Together, and friends. These offer ``response_format={"type": "json_object"}``
  only: valid JSON is guaranteed, the *shape* is not.

**That capability gap is contained, not papered over.** For json-object providers we put the schema
and a worked example in the prompt, and then — as always — ``ExtractionResult.model_validate_json``
is the gate. A provider that returns the wrong shape fails validation and the batch is refused; it
cannot produce a half-parsed assertion. This is exactly the division of labor working: agents propose, code decides.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

#: Anthropic. Adaptive thinking + strict schema.
ANTHROPIC_DEFAULT_MODEL = "claude-opus-4-8"

#: DeepSeek V4. `-pro` is the default for the accuracy-critical extraction stage; `-flash` is ~3x
#: cheaper and also V4 (1M ctx) if throughput matters more than recall — pass --model to switch.
#: NB `deepseek-chat` / `deepseek-reasoner` are deprecated (2026-07-24) aliases onto V4-Flash; we
#: name a V4 model explicitly so nothing breaks when they are withdrawn.
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-pro"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

#: How many extra times to re-issue a json_object request that came back with EMPTY content. DeepSeek
#: documents that json_object mode intermittently returns an empty body, and v4-pro does it often
#: enough to abort a whole harvest (#4). An empty body is a provider hiccup, not "the document says
#: nothing" (that is a well-formed `{"drafts": []}`), so a bounded retry recovers it instead of failing
#: the dataset. A real API error is not retried here — it raises on the first attempt.
_EMPTY_CONTENT_RETRIES = 3


class ProviderUnavailable(RuntimeError):
    """No usable provider: SDK missing, credential absent, or the endpoint failed."""


@dataclass(frozen=True)
class LLMResponse:
    """Raw model output plus normalized usage. The text is UNVALIDATED — the caller decides."""

    text: str
    usage: dict[str, int]
    #: How the call was made, recorded for the cost/provenance ledger: reasoning ``thinking`` mode,
    #: the ``max_tokens`` ceiling, and which structured-output ``response_format`` was in force. The
    #: same prompt at a different effort is a different run, and this is what lets a reader see it.
    mode: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    """What extraction needs from a model: JSON text back, and a name to record in provenance."""

    name: str

    def default_model(self) -> str: ...

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], model: str, max_tokens: int
    ) -> LLMResponse: ...


class AnthropicProvider:
    """Claude via the official SDK: strict schema, explicit prefix caching, adaptive thinking."""

    name = "anthropic"

    def __init__(self, *, client: Any | None = None, api_key: str | None = None) -> None:
        self._client = client
        self._api_key = api_key

    def default_model(self) -> str:
        return ANTHROPIC_DEFAULT_MODEL

    def _resolve(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - host dependent
            raise ProviderUnavailable("the `anthropic` SDK is not installed") from exc
        return (
            anthropic.Anthropic(api_key=self._api_key) if self._api_key else anthropic.Anthropic()
        )

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], model: str, max_tokens: int
    ) -> LLMResponse:
        client = self._resolve()
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                # cache breakpoint on the last system block: render order is tools -> system ->
                # messages, so the stable prefix caches and the volatile document does not.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
                thinking={"type": "adaptive"},
            )
        except Exception as exc:
            raise ProviderUnavailable(f"anthropic call failed: {exc}") from exc
        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
        return LLMResponse(
            text=text,
            usage=_anthropic_usage(response),
            mode={
                "thinking": "adaptive",
                "max_tokens": max_tokens,
                "response_format": "json_schema",
            },
        )


class OpenAICompatibleProvider:
    """Any OpenAI-shaped endpoint (DeepSeek, vLLM, Ollama, Together, ...) selected by ``base_url``.

    These expose ``json_object`` mode only, so the schema travels in the prompt and Pydantic — not
    the provider — enforces the shape. Prefix caching is automatic server-side (DeepSeek reports it
    as cache hit/miss tokens), so there is no ``cache_control`` to place; keeping the prefix stable
    is the whole job.
    """

    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        default_model: str = DEEPSEEK_DEFAULT_MODEL,
        name: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.base_url = base_url
        self._api_key = api_key
        self._default_model = default_model
        self._client = client
        if name:
            self.name = name

    def default_model(self) -> str:
        return self._default_model

    def _resolve(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - host dependent
            raise ProviderUnavailable(
                "the `openai` SDK is not installed (it is the client for OpenAI-compatible "
                "endpoints such as DeepSeek)"
            ) from exc
        if not self._api_key:
            raise ProviderUnavailable(f"no API key for {self.name} ({self.base_url})")
        return OpenAI(api_key=self._api_key, base_url=self.base_url)

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], model: str, max_tokens: int
    ) -> LLMResponse:
        client = self._resolve()
        # Re-issue on EMPTY content, bounded (#4). DeepSeek's json_object mode intermittently returns
        # an empty body; that is a provider hiccup, not the document saying nothing (which is a
        # well-formed `{"drafts": []}`), so a few retries recover it instead of aborting the harvest.
        # Usage is ACCUMULATED across attempts: an empty response still cost tokens, and the harvest
        # ledger is meant to reflect what the calls actually cost, not just the final one.
        usage_total: dict[str, int] = {}
        for _ in range(_EMPTY_CONTENT_RETRIES + 1):
            try:
                response = client.chat.completions.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    # json_object guarantees valid JSON, NOT the right shape — Pydantic checks shape.
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                # A real API error is not a content hiccup — fail on the first attempt, do not retry.
                raise ProviderUnavailable(f"{self.name} call failed: {exc}") from exc
            for key, val in _openai_usage(response).items():
                usage_total[key] = usage_total.get(key, 0) + val
            choice = response.choices[0] if response.choices else None
            text = (getattr(choice.message, "content", None) or "") if choice else ""
            if text.strip():
                # `thinking` is the MODEL's own (v4-pro/-reasoner reason inherently); the API takes no
                # toggle, so it is reported as the model name's business, not a flag we set.
                # `response_format` is the weaker json_object contract, which is why Pydantic — not the
                # provider — enforces the shape.
                return LLMResponse(
                    text=text,
                    usage=usage_total,  # summed over every attempt, including the empty ones
                    mode={
                        "thinking": "model-default",
                        "max_tokens": max_tokens,
                        "response_format": "json_object",
                    },
                )
        # Every attempt came back empty: refuse loudly rather than let it read as "says nothing". The
        # hint is provider-agnostic (this class also serves vLLM/Ollama/Together) and names no
        # soon-deprecated model.
        raise ProviderUnavailable(
            f"{self.name} returned empty content in JSON mode on {_EMPTY_CONTENT_RETRIES + 1} "
            f"attempts (a known json_object-mode failure; try a different model or provider)"
        )


def deepseek_provider(api_key: str | None = None, **kwargs: Any) -> OpenAICompatibleProvider:
    """DeepSeek preset of the OpenAI-compatible provider."""
    return OpenAICompatibleProvider(
        base_url=DEEPSEEK_BASE_URL,
        api_key=api_key or os.environ.get("DEEPSEEK_API_KEY"),
        default_model=DEEPSEEK_DEFAULT_MODEL,
        name="deepseek",
        **kwargs,
    )


def resolve_provider(name: str | None = None) -> LLMProvider:
    """Pick a provider explicitly, or auto-detect from the environment.

    Explicit beats implicit: ``--provider`` / ``SEQFORGE_LLM_PROVIDER`` wins. Otherwise we take
    whichever credential is present, and refuse (listing the options) rather than guess when neither
    is — an extraction that silently picks a different model than you expected is a provenance bug.
    """
    choice = (name or os.environ.get("SEQFORGE_LLM_PROVIDER") or "").strip().lower()
    if choice in ("deepseek", "deepseek-v4"):
        return deepseek_provider()
    if choice == "anthropic":
        return AnthropicProvider()
    if choice in ("openai-compatible", "custom"):
        base = os.environ.get("SEQFORGE_LLM_BASE_URL")
        if not base:
            raise ProviderUnavailable("provider 'openai-compatible' needs SEQFORGE_LLM_BASE_URL")
        return OpenAICompatibleProvider(
            base_url=base,
            api_key=os.environ.get("SEQFORGE_LLM_API_KEY"),
            default_model=os.environ.get("SEQFORGE_LLM_MODEL", DEEPSEEK_DEFAULT_MODEL),
            name="openai-compatible",
        )
    if choice:
        raise ProviderUnavailable(
            f"unknown provider {choice!r}; known: anthropic, deepseek, openai-compatible"
        )

    if os.environ.get("DEEPSEEK_API_KEY"):
        return deepseek_provider()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicProvider()
    raise ProviderUnavailable(
        "no LLM credential found. Set DEEPSEEK_API_KEY or ANTHROPIC_API_KEY, or pass "
        "--provider with SEQFORGE_LLM_BASE_URL/-API_KEY for any OpenAI-compatible endpoint."
    )


def _anthropic_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        "cache_write_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    }


def _openai_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    out = {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }
    # DeepSeek reports automatic prefix caching this way; normalize onto the common key.
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    if hit is not None:
        out["cache_read_tokens"] = int(hit or 0)
    return out


def schema_prompt(schema: dict[str, Any]) -> str:
    """The json-mode contract: say 'json', show the schema, show an example (DeepSeek requires both).

    Harmless on providers that enforce a strict schema, so extraction keeps ONE prompt across
    providers — one ``prompt_version``, one thing for evals to compare.
    """
    example = {
        "drafts": [
            {
                "field": "library.chemistry",
                "value": "10x-3p-gex-v3",
                "span": {
                    "doc_sha256": "<echo the sha given below>",
                    "quote": "Chromium Single Cell 3' v3",
                    "context": None,
                },
                "llm_confidence": 0.95,
            }
        ]
    }
    return (
        "Return a single json object matching this JSON Schema exactly:\n"
        f"{json.dumps(schema, separators=(',', ':'))}\n\n"
        "Example of a well-formed json response (an empty `drafts` list is valid and common):\n"
        f"{json.dumps(example, indent=2)}\n"
    )
