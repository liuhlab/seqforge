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

from ..io.remote import _RETRY_STATUS

#: Anthropic. Adaptive thinking + strict schema.
ANTHROPIC_DEFAULT_MODEL = "claude-opus-4-8"

#: DeepSeek V4, the two models the API serves. Both are V4 (1M ctx) and speak the same json_object
#: contract; they differ in price and in recall on hard prose. NB `deepseek-chat` /
#: `deepseek-reasoner` are deprecated (2026-07-24) aliases onto V4-Flash; we name a V4 model
#: explicitly so nothing breaks when they are withdrawn.
DEEPSEEK_FLASH_MODEL = "deepseek-v4-flash"
DEEPSEEK_PRO_MODEL = "deepseek-v4-pro"

#: What `--model` may name on the DeepSeek preset — a **catalogue, not an allowlist**. The model
#: string is passed through unchecked because the same adapter serves arbitrary OpenAI-shaped
#: endpoints whose models we cannot enumerate, and because a name DeepSeek ships tomorrow must not
#: need a release here. An unknown name comes back as a 400: a verdict, not retried (see
#: `classify_api_error`).
DEEPSEEK_MODELS = (DEEPSEEK_FLASH_MODEL, DEEPSEEK_PRO_MODEL)

#: `-pro` is the default because `-flash` was measured (#188). The old default rested on "~3x cheaper
#: at the same V4 quality bar, which is the lever that matters across 10⁴ datasets"; run head-to-head
#: over the benchmark corpus, pro won every axis including the two flash was chosen for — faster, and
#: FEWER output tokens, on the same input. Correctness was never the axis in question (R2 re-greps
#: every quote whichever model proposed it), so this is a cost decision that flipped when someone
#: measured the cost. The run, its numbers and its caveats: `evals/README.md`, "The default model,
#: and the run that decided it".
DEEPSEEK_DEFAULT_MODEL = DEEPSEEK_PRO_MODEL
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

#: Exception *type* names that mean the call never reached a verdict — the transport gave out. Matched
#: by name rather than by class because both SDKs are optional imports: naming `openai.APITimeoutError`
#: here would make classification depend on the SDK being installed, which is exactly the terminal
#: condition we are trying to tell apart from a blip.
_TRANSIENT_EXC_NAMES = ("timeout", "connection", "remotedisconnected", "protocolerror")


class ProviderUnavailable(RuntimeError):
    """No usable provider: SDK missing, credential absent, or the endpoint failed.

    Carries the classification the retry needs, because **only the provider can make it**. By the time
    this reaches the caller the SDK exception is gone, and "no credential" and "429" look identical —
    a loop that cannot tell them apart backs off four times over a missing API key.

    - ``transient`` — worth another attempt. Set *only* where the provider held the real exception.
    - ``retry_after`` — seconds to wait, as the header string ``retry_delay`` already parses.
      ``"0"`` means retry at once: an empty body is a content hiccup with nothing to wait for, while a
      429 is the endpoint asking for room. One loop, two paces, expressed through the existing knob.
    - ``usage`` — what the failed attempt cost. A refused call still burns tokens, and the ledger is
      meant to say what the calls actually cost rather than what the last one did.
    """

    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        retry_after: str | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.retry_after = retry_after
        self.usage = usage or {}


def classify_api_error(exc: Exception) -> tuple[bool, str | None]:
    """Is this SDK exception worth retrying, and how long should the caller wait?

    Duck-typed on purpose. Both SDKs raise status-carrying errors shaped like httpx's, so reading
    ``status_code`` and the ``Retry-After`` header covers them without importing either. The status
    set is the network surface's — a rate limit and the 5xx family are blips; a 400 or a 401 is a
    verdict, and retrying it four times only makes the failure slower.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        headers: Any = getattr(getattr(exc, "response", None), "headers", None) or {}
        raw = headers.get("retry-after") if hasattr(headers, "get") else None
        return status in _RETRY_STATUS, str(raw) if raw is not None else None
    name = type(exc).__name__.lower()
    return any(token in name for token in _TRANSIENT_EXC_NAMES), None


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
            transient, retry_after = classify_api_error(exc)
            raise ProviderUnavailable(
                f"anthropic call failed: {exc}", transient=transient, retry_after=retry_after
            ) from exc
        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
        usage = _anthropic_usage(response)
        if not text.strip():
            # A thinking-only turn, or a stop before any text block, leaves no JSON to parse. Left as
            # `text=""` this surfaced one turn later as "returned output that is not valid JSON" — a
            # shape complaint about a response that had no shape. It is the same empty-body hiccup the
            # json-object path already recovers from, so it is refused the same way and retried at
            # once: there is no rate limit to respect, only a turn that produced nothing.
            raise ProviderUnavailable(
                "anthropic returned no text block (a thinking-only or empty turn)",
                transient=True,
                retry_after="0",
                usage=usage,
            )
        return LLMResponse(
            text=text,
            usage=usage,
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
        # Check the credential BEFORE importing the SDK. A missing key cannot construct a client, so
        # importing `openai` first would only pay the (heavy) import cost to raise the same error — and
        # the key-check path must not depend on the SDK being installed at all.
        if not self._api_key:
            raise ProviderUnavailable(f"no API key for {self.name} ({self.base_url})")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - host dependent
            raise ProviderUnavailable(
                "the `openai` SDK is not installed (it is the client for OpenAI-compatible "
                "endpoints such as DeepSeek)"
            ) from exc
        return OpenAI(api_key=self._api_key, base_url=self.base_url)

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], model: str, max_tokens: int
    ) -> LLMResponse:
        client = self._resolve()
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
            transient, retry_after = classify_api_error(exc)
            raise ProviderUnavailable(
                f"{self.name} call failed: {exc}", transient=transient, retry_after=retry_after
            ) from exc
        usage = _openai_usage(response)
        choice = response.choices[0] if response.choices else None
        text = (getattr(choice.message, "content", None) or "") if choice else ""
        if not text.strip():
            # DeepSeek's json_object mode intermittently returns an empty body, often enough to abort a
            # whole harvest (#4). That is a provider hiccup, not the document saying nothing (which is
            # a well-formed `{"drafts": []}`). The retry lives one level up now, so this says *what
            # happened* and *that it is worth another go* rather than looping here — one budget, shared
            # with the transient-API case, instead of a second one nested inside it. `retry_after="0"`
            # because an empty body asks for nothing; there is no rate limit to respect.
            raise ProviderUnavailable(
                f"{self.name} returned empty content in JSON mode (a known json_object-mode "
                f"failure; try a different model or provider)",
                transient=True,
                retry_after="0",
                usage=usage,
            )
        # `thinking` is the MODEL's own (how much a V4 reasons is baked into -flash vs -pro); the API
        # takes no toggle, so it is the model name's business, not a flag we set. `response_format` is
        # the weaker json_object contract: Pydantic — not the provider — enforces the shape.
        return LLMResponse(
            text=text,
            usage=usage,
            mode={
                "thinking": "model-default",
                "max_tokens": max_tokens,
                "response_format": "json_object",
            },
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
    """Anthropic's three input buckets, normalized onto the common keys.

    **``input_tokens`` is EVERY input token the request was billed for**, and the two cache keys are
    a breakdown of that same total rather than extra tokens beside it. Anthropic reports three
    disjoint buckets — uncached, cache-creation, cache-read — where DeepSeek's ``prompt_tokens``
    is already the inclusive figure, so summing the raw fields would make one usage key mean two
    different things depending on who answered. A ceiling at a pluggable seam cannot survive that:
    the same run would read as 3.5M tokens on one provider and 675K on the other.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    fresh = int(getattr(usage, "input_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    return {
        "input_tokens": fresh + cache_read + cache_write,
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }


def _openai_usage(response: Any) -> dict[str, int]:
    """The OpenAI shape, on the same convention: ``prompt_tokens`` already includes the cache hits."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    out = {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }
    # DeepSeek reports automatic prefix caching this way; normalize onto the common key. It is a
    # BREAKDOWN of `input_tokens`, not an addition to it — these endpoints bill cached input inside
    # `prompt_tokens`, and the Anthropic normalizer above is folded to match.
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
